"""Simulation : effet d'un ou plusieurs nouveaux services d'urgences.

Pour un site candidat on lance un Dijkstra borné (LIMITE_MIN) depuis son nœud
sur le graphe inversé : on obtient le temps « habitant → nouveau site » pour
tous les carreaux atteignables. Le nouveau temps d'accès d'un carreau est le
minimum entre son temps actuel et celui vers chaque site simulé.
"""
import threading
from collections import OrderedDict

import numpy as np

from .territoire import CODE_SANS_ACCES, MINUTES_PLAFOND, SEUIL_MIN, SNAP_MAX_M, coder, rassembler

LIMITE_MIN = 75  # au-delà, un nouveau site n'améliore plus rien de significatif


class Simulateur:
    def __init__(self, T, taille_cache=64):
        self.T = T
        self.cache = OrderedDict()
        self.taille_cache = taille_cache
        self.verrou = threading.Lock()  # le cache LRU est partagé entre les requêtes

    def impact(self, lon, lat):
        """Carreaux et pixels améliorés par un site placé en (lon, lat)."""
        with self.verrou:
            return self._impact(lon, lat)

    def _impact(self, lon, lat):
        cle = (round(lon, 4), round(lat, 4))
        if cle in self.cache:
            self.cache.move_to_end(cle)
            return self.cache[cle]
        T = self.T
        nd, appr, d = T.reseau.accrocher(lon, lat)
        if d[0] > SNAP_MAX_M:
            return None
        # Dijkstra élagué : seuls les nœuds dont le temps s'améliore sont explorés.
        noeuds, t_noeuds = T.elagueur.ameliores(nd[0], appr[0], LIMITE_MIN * 60)

        cel, tn = rassembler(T.idx_cel, noeuds, t_noeuds)
        m = (tn + T.cel_appr[cel]) / 60
        mieux = m < T.min_cel0[cel]
        cel, m = cel[mieux], m[mieux].astype(np.float32)

        pix, tp = rassembler(T.idx_pix, noeuds, t_noeuds)
        codes = coder((tp + T.pix_appr[pix]) / 60)
        mieux = codes < T.code_pix0[pix]
        pix, codes = pix[mieux], codes[mieux]
        patch = None
        if len(pix):
            flat = T.pix[pix]
            r, c = flat // T.W, flat % T.W
            r0, c0 = int(r.min()), int(c.min())
            h, w = int(r.max()) - r0 + 1, int(c.max()) - c0 + 1
            buf = np.full((h, w), CODE_SANS_ACCES, np.uint8)
            buf[r - r0, c - c0] = codes
            patch = {"x": c0, "y": r0, "w": w, "h": h, "data": buf}
        res = {"cel": cel, "min": m, "patch": patch, "noeud": int(nd[0]),
               "lon": float(T.reseau.lon[nd[0]]), "lat": float(T.reseau.lat[nd[0]])}
        self.cache[cle] = res
        if len(self.cache) > self.taille_cache:
            self.cache.popitem(last=False)
        return res

    def minutes(self, sites):
        """Temps d'accès de chaque carreau avec les sites simulés."""
        m = self.T.min_cel0.copy()
        for lon, lat in sites:
            imp = self.impact(lon, lat)
            if imp is not None and len(imp["cel"]):
                m[imp["cel"]] = np.minimum(m[imp["cel"]], imp["min"])
        return m


def statistiques(T, minutes, seuil=SEUIL_MIN):
    """Indicateurs par département (et par commune en outre-mer)."""
    pop = T.cel_pop
    loin = ~(minutes < seuil)
    mc = np.minimum(minutes, MINUTES_PLAFOND)
    nb = int(T.cel_dep.max()) + 1
    p = np.bincount(T.cel_dep, pop, nb)
    pl = np.bincount(T.cel_dep, pop * loin, nb)
    pm = np.bincount(T.cel_dep, pop * mc, nb)
    deps = {}
    for d in np.unique(T.cel_dep):
        if p[d] > 0:
            deps[int(d)] = {"pop": float(p[d]), "pop_loin": float(pl[d]), "moy": float(pm[d] / p[d]),
                            "part_loin": float(pl[d] / p[d])}
    out = {"pop": float(pop.sum()), "pop_loin": float((pop * loin).sum()),
           "moy": float((pop * mc).sum() / pop.sum()), "deps": deps}
    if len(T.cel_commune):
        out["communes"] = {str(c): {"pop": float(pop[i]), "moy": float(mc[i]), "loin": bool(loin[i])}
                           for i, c in enumerate(T.cel_commune)}
    return out
