"""Un territoire = réseau routier + sites d'urgences + population + raster d'affichage.

`construire()` (pipeline, une fois) précalcule l'accrochage des carreaux de
population et des pixels du raster au réseau, ainsi que les temps de base.
`charger()` (API) relit ce modèle en quelques secondes.
"""
import json
from pathlib import Path

import numpy as np
import shapely

from .dijkstra import Elagueur
from .reseau import Reseau

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "data" / "build"

SEUIL_MIN = 30
SNAP_MAX_M = 6000.0           # au-delà, le point est considéré hors réseau routier
DESTINATIONS = {"SU", "AMU"}   # structures des urgences générales + antennes
SMUR = {"SMUR", "SMURP", "ASMUR"}
MINUTES_PLAFOND = 120.0       # temps retenu pour un habitant sans accès routier (moyennes)

# Raster : 1 octet par pixel = minutes × 2 (0..250) ; 254 = hors terre ; 255 = sans accès routier.
CODE_HORS, CODE_SANS_ACCES = 254, 255

BBOX = {"FR": (-5.3, 41.25, 9.7, 51.2)}


def merc(lat):
    return np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))


def inv_merc(y):
    return np.degrees(2 * np.arctan(np.exp(y)) - np.pi / 2)


def coder(minutes):
    m = np.asarray(minutes, np.float32)
    c = np.where(np.isfinite(m), np.minimum(np.round(m * 2), 250), CODE_SANS_ACCES)
    return c.astype(np.uint8)


def index_par_noeud(noeud_de, n):
    """CSR « nœud → éléments accrochés à ce nœud » (carreaux ou pixels)."""
    ok = np.flatnonzero(noeud_de >= 0)
    ordre = ok[np.argsort(noeud_de[ok], kind="stable")]
    ptr = np.zeros(n + 1, np.int64)
    ptr[1:] = np.cumsum(np.bincount(noeud_de[ok], minlength=n))
    return ptr, ordre


def rassembler(index, noeuds, valeurs):
    """Éléments accrochés aux `noeuds` et valeur de leur nœud."""
    ptr, items = index
    a, b = ptr[noeuds], ptr[noeuds + 1]
    nb = b - a
    tot = int(nb.sum())
    if not tot:
        return np.array([], np.int64), np.array([], valeurs.dtype)
    pos = np.repeat(a - np.cumsum(np.r_[0, nb[:-1]]), nb) + np.arange(tot)
    return items[pos], np.repeat(valeurs, nb)


class Territoire:
    def __init__(self, code, cfg):
        self.code, self.cfg = code, cfg
        self.reseau = Reseau(BUILD / f"graphe_{code}.npz", cfg["epsg"])

    # ------------------------------------------------------------------ build
    @classmethod
    def construire(cls, code, cfg, deps_meta, dep_geoms, sites):
        T = cls(code, cfg)
        R = T.reseau
        if "deps" in cfg:  # sous-ensemble (tests)
            dans = lambda d: d in cfg["deps"]
        else:
            dans = (lambda d: not d.startswith(("97", "98"))) if code == "FR" else (lambda d: d == cfg["dep"])
        idx_deps = [i for i, m in enumerate(deps_meta) if dans(m["code"])]

        # Sites d'urgences (destinations) et SMUR, accrochés au réseau.
        sites = [s for s in sites if dans(s["dep"])]
        dest = [s for s in sites if DESTINATIONS & set(s["types"])]
        n_dest, _, d_dest = R.accrocher(np.array([s["lon"] for s in dest]), np.array([s["lat"] for s in dest]))

        # Population.
        z = np.load(BUILD / f"pop_{cfg.get('pop', code)}.npz")
        garde = np.isin(z["dep"], idx_deps)
        p = {k: z[k][garde] if len(z[k]) == len(garde) else z[k] for k in z.files}
        n_cel, a_cel, d_cel = R.accrocher(p["lon"], p["lat"])
        n_cel[d_cel > SNAP_MAX_M] = -1

        # Raster d'affichage en projection Web Mercator (aligné sur la carte).
        terre = shapely.union_all(shapely.make_valid(np.array([dep_geoms[i] for i in idx_deps], dtype=object)))
        if code in BBOX:
            b = BBOX[code]
        else:
            x0, y0, x1, y1 = terre.bounds
            mx, my = (x1 - x0) * 0.04, (y1 - y0) * 0.04
            b = (x0 - mx, y0 - my, x1 + mx, y1 + my)
        ym0, ym1 = merc(b[3]), merc(b[1])
        larg_m = (b[2] - b[0]) * 111320 * np.cos(np.radians((b[1] + b[3]) / 2))
        W = int(np.ceil(larg_m / cfg["res_m"]))
        H = int(round(W * (ym0 - ym1) / np.radians(b[2] - b[0])))
        lon_c = b[0] + (np.arange(W) + 0.5) / W * (b[2] - b[0])
        lat_c = inv_merc(ym0 - (np.arange(H) + 0.5) / H * (ym0 - ym1))
        LON, LAT = np.meshgrid(lon_c, lat_c)
        shapely.prepare(terre)
        sur_terre = shapely.contains_xy(terre, LON.ravel(), LAT.ravel())
        pix = np.flatnonzero(sur_terre).astype(np.int32)
        n_pix, a_pix, d_pix = R.accrocher(LON.ravel()[pix], LAT.ravel()[pix])
        n_pix[d_pix > SNAP_MAX_M] = -1

        np.savez_compressed(
            BUILD / f"modele_{code}.npz",
            dest_noeud=n_dest, dest_dist=d_dest.astype(np.float32),
            cel_noeud=n_cel, cel_appr=a_cel, cel_pop=p["pop"], cel_dep=p["dep"],
            cel_lon=p["lon"].astype(np.float32), cel_lat=p["lat"].astype(np.float32),
            cel_commune=p["commune"] if "commune" in p else np.array([]),
            pix=pix, pix_noeud=n_pix, pix_appr=a_pix, W=W, H=H, bbox=np.array(b),
            deps=np.array(idx_deps, np.int32))
        (BUILD / f"sites_{code}.json").write_text(json.dumps(
            {"destinations": dest, "smur": [s for s in sites if SMUR & set(s["types"])]}, ensure_ascii=False))
        return cls.charger(code, cfg, T)

    # ------------------------------------------------------------------- load
    @classmethod
    def charger(cls, code, cfg, T=None):
        T = T or cls(code, cfg)
        z = np.load(BUILD / f"modele_{code}.npz")
        for k in z.files:
            setattr(T, k, z[k])
        T.W, T.H = int(T.W), int(T.H)
        s = json.loads((BUILD / f"sites_{code}.json").read_text())
        T.destinations, T.smur = s["destinations"], s["smur"]
        T.ok_cel = T.cel_noeud >= 0
        T.ok_pix = T.pix_noeud >= 0
        T.recalculer_base()
        T.idx_cel = index_par_noeud(T.cel_noeud, T.reseau.n)
        T.idx_pix = index_par_noeud(T.pix_noeud, T.reseau.n)
        T.elagueur = Elagueur(T.reseau.G, T.T_noeud)
        return T

    def recalculer_base(self):
        """Temps de base : un seul Dijkstra multi-sources depuis tous les services."""
        ok = self.dest_dist <= SNAP_MAX_M
        self.T_noeud, rang = self.reseau.temps_vers(self.dest_noeud[ok])
        self.dest_ok = np.flatnonzero(ok)
        self.plus_proche_noeud = rang
        self.min_cel0 = self.minutes_cellules(self.T_noeud)
        self.code_pix0 = self.codes_pixels(self.T_noeud)

    # ---------------------------------------------------------------- helpers
    def minutes_cellules(self, T_noeud, sel=None):
        nd = self.cel_noeud if sel is None else self.cel_noeud[sel]
        ap = self.cel_appr if sel is None else self.cel_appr[sel]
        t = np.where(nd >= 0, T_noeud[np.maximum(nd, 0)] + ap, np.inf)
        return (t / 60).astype(np.float32)

    def codes_pixels(self, T_noeud):
        t = np.where(self.ok_pix, T_noeud[np.maximum(self.pix_noeud, 0)] + self.pix_appr, np.inf)
        return coder(t / 60)

    def raster(self, codes_pix):
        r = np.full(self.W * self.H, CODE_HORS, np.uint8)
        r[self.pix] = codes_pix
        return r.reshape(self.H, self.W)

    def plus_proche_site(self, cel_idx):
        nd = self.cel_noeud[cel_idx]
        r = self.plus_proche_noeud[np.maximum(nd, 0)]
        return np.where(nd >= 0, r, -1)
