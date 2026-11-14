"""Étape 2 — Graphe routier orienté à partir d'OpenStreetMap (Geofabrik).

  1. `osmium tags-filter` ne garde que les voies carrossables utiles ;
  2. pyosmium lit les voies avec leurs coordonnées ;
  3. chaque voie est découpée aux intersections (et tous les PAS_SEGMENT_M mètres) ;
  4. chaque arc reçoit un temps de parcours = longueur / vitesse, la vitesse
     venant du tag maxspeed (× 0,8 comme OSRM) ou du profil par type de voie ;
  5. les sens uniques (oneway, giratoires) sont respectés.

Usage : python roads.py FR  (ou GP, MQ, GF, RE, YT, ou un .pbf de test)
"""
import subprocess
import sys
import time
from array import array

import numpy as np
import osmium

from config import (BUILD, MAXSPEED_SYMBOLES, PAS_SEGMENT_M, RAW, REDUCTION_MAXSPEED,
                    TERRITOIRES, VITESSES)

CLASSES = list(VITESSES)
FILTRE = "w/highway=" + ",".join(CLASSES)
INTERDIT = {"no", "private", "agricultural", "forestry", "delivery", "destination_no"}


def vitesse(tags, hw):
    """Vitesse moyenne de circulation (km/h) d'une voie."""
    base = VITESSES[hw]
    ms = tags.get("maxspeed")
    if ms:
        v = MAXSPEED_SYMBOLES.get(ms)
        if v is None:
            try:
                v = float(ms.split()[0].replace(",", "."))
                if "mph" in ms:
                    v *= 1.609
            except ValueError:
                v = None
        if v and 5 <= v <= 150:
            # Comme OSRM : on roule à ~80 % de la limite, sans dépasser le profil
            # de la classe de route de plus de 30 % (évite les tags aberrants).
            return max(5.0, min(v * REDUCTION_MAXSPEED, base * 1.3 if hw.endswith("link") else 130))
    return float(base)


def sens(tags, hw):
    """1 = sens direct seulement, -1 = sens inverse seulement, 0 = double sens."""
    ow = tags.get("oneway")
    if ow in ("yes", "true", "1"):
        return 1
    if ow == "-1":
        return -1
    if ow in ("no", "false", "0"):
        return 0
    if tags.get("junction") in ("roundabout", "circular") or hw in ("motorway", "motorway_link"):
        return 1
    return 0


class Lecteur(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.refs, self.lon, self.lat = array("q"), array("d"), array("d")
        self.debut, self.vit, self.sens, self.cls = array("q"), array("f"), array("b"), array("b")

    def way(self, w):
        t = w.tags
        hw = t.get("highway")
        if hw not in VITESSES or t.get("area") == "yes":
            return
        acc = t.get("motor_vehicle") or t.get("motorcar") or t.get("vehicle") or t.get("access")
        if acc in INTERDIT:
            return
        nds = w.nodes
        if len(nds) < 2:
            return
        r, x, y = [], [], []
        for n in nds:
            loc = n.location
            if not loc.valid():  # voie coupée par la limite de l'extrait
                return
            r.append(n.ref)
            x.append(loc.lon)
            y.append(loc.lat)
        self.debut.append(len(self.refs))
        self.refs.extend(r)
        self.lon.extend(x)
        self.lat.extend(y)
        self.vit.append(vitesse(t, hw))
        self.sens.append(sens(t, hw))
        self.cls.append(CLASSES.index(hw))


def haversine(lon1, lat1, lon2, lat2):
    r = np.pi / 180
    a = np.sin((lat2 - lat1) * r / 2) ** 2 + np.cos(lat1 * r) * np.cos(lat2 * r) * np.sin((lon2 - lon1) * r / 2) ** 2
    return 2 * 6371008.8 * np.arcsin(np.sqrt(a))


def construire(pbf_path, sortie):
    t0 = time.time()
    filtre = BUILD / (sortie + "-routes.osm.pbf")
    BUILD.mkdir(parents=True, exist_ok=True)
    subprocess.run(["osmium", "tags-filter", "-O", "-o", str(filtre), str(pbf_path), FILTRE], check=True)
    print(f"  filtrage osmium : {time.time() - t0:.0f}s ({filtre.stat().st_size / 1e6:.0f} Mo)")

    h = Lecteur()
    h.apply_file(str(filtre), locations=True, idx="flex_mem")
    refs = np.frombuffer(h.refs, dtype=np.int64)
    lon = np.frombuffer(h.lon, dtype=np.float64)
    lat = np.frombuffer(h.lat, dtype=np.float64)
    debut = np.append(np.frombuffer(h.debut, dtype=np.int64), len(refs))
    vit = np.frombuffer(h.vit, dtype=np.float32)
    sns = np.frombuffer(h.sens, dtype=np.int8)
    cls = np.frombuffer(h.cls, dtype=np.int8)
    nw = len(vit)
    print(f"  lecture : {nw:,} voies, {len(refs):,} points ({time.time() - t0:.0f}s)")

    # Nœuds à conserver : extrémités de voie, intersections (nœud partagé), puis
    # un point tous les PAS_SEGMENT_M mètres pour un accrochage précis.
    uniq, inv, cnt = np.unique(refs, return_inverse=True, return_counts=True)
    garde = cnt[inv] > 1
    garde[debut[:-1]] = True
    garde[debut[1:] - 1] = True
    way_id = np.repeat(np.arange(nw), np.diff(debut))
    seg = np.zeros(len(refs))
    seg[1:] = haversine(lon[:-1], lat[:-1], lon[1:], lat[1:])
    seg[debut[:-1]] = 0.0  # pas de segment entre deux voies différentes
    cum = np.cumsum(seg)
    # Découpage régulier : on garde le premier point qui franchit chaque multiple du pas.
    base_way = cum[debut[:-1]][way_id]
    tranche = np.floor((cum - base_way) / PAS_SEGMENT_M).astype(np.int64)
    saut = np.zeros(len(refs), bool)
    saut[1:] = (tranche[1:] != tranche[:-1]) & (way_id[1:] == way_id[:-1])
    garde |= saut

    # Arcs entre deux points conservés consécutifs d'une même voie.
    idx = np.flatnonzero(garde)
    meme = way_id[idx[1:]] == way_id[idx[:-1]]
    a, b = idx[:-1][meme], idx[1:][meme]
    w_of = way_id[a]
    longueur = cum[b] - cum[a]
    duree = longueur / (vit[w_of] / 3.6)
    noeud = inv  # identifiant compact du nœud OSM
    u, v = noeud[a], noeud[b]
    s = sns[w_of]
    U = np.concatenate([u[s >= 0], v[s <= 0]])
    V = np.concatenate([v[s >= 0], u[s <= 0]])
    T = np.concatenate([duree[s >= 0], duree[s <= 0]]).astype(np.float32)
    L = np.concatenate([longueur[s >= 0], longueur[s <= 0]]).astype(np.float32)
    C = np.concatenate([cls[w_of][s >= 0], cls[w_of][s <= 0]])

    # Renumérotation des nœuds réellement utilisés.
    utiles = np.unique(np.concatenate([U, V]))
    remap = np.full(len(uniq), -1, np.int64)
    remap[utiles] = np.arange(len(utiles))
    first = np.zeros(len(uniq), np.int64)
    first[inv[::-1]] = np.arange(len(refs))[::-1]
    nlon, nlat = lon[first[utiles]], lat[first[utiles]]
    U, V = remap[U].astype(np.int32), remap[V].astype(np.int32)
    ok = U != V
    U, V, T, L, C = U[ok], V[ok], T[ok], L[ok], C[ok]
    np.savez_compressed(BUILD / f"graphe_{sortie}.npz", lon=nlon, lat=nlat, u=U, v=V, t=T, l=L, c=C,
                        classes=np.array(CLASSES))
    print(f"  graphe : {len(nlon):,} nœuds, {len(U):,} arcs, {L.sum() / 1e3 / 2:,.0f} km env. "
          f"({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    arg = sys.argv[1]
    if arg in TERRITOIRES:
        construire(RAW / TERRITOIRES[arg]["pbf"], arg)
    else:
        construire(arg, sys.argv[2])
