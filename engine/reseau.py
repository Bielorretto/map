"""Réseau routier : chargement du graphe, accrochage (snapping) et plus courts chemins.

Le graphe est stocké INVERSÉ (arc v→u pour chaque tronçon u→v) : un Dijkstra
lancé depuis un hôpital donne alors, pour chaque nœud, le temps de trajet
« nœud → hôpital » en respectant les sens uniques (direction arrivée).
"""
import numpy as np
from pyproj import Transformer
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components, dijkstra
from scipy.spatial import cKDTree

TAILLE_MIN_COMPOSANTE = 200  # ignore les îlots de voirie déconnectés
VITESSE_APPROCHE_MS = 20 / 3.6
DETOUR_APPROCHE = 1.3


class Reseau:
    def __init__(self, chemin, epsg):
        z = np.load(chemin)
        self.lon, self.lat = z["lon"], z["lat"]
        n = len(self.lon)
        u, v, t = z["u"], z["v"], np.maximum(z["t"].astype(np.float64), 0.05)
        # Arcs parallèles : on garde le plus rapide (csr_matrix additionnerait les doublons).
        o = np.lexsort((t, v, u))
        u, v, t = u[o], v[o], t[o]
        prem = np.ones(len(u), bool)
        prem[1:] = (u[1:] != u[:-1]) | (v[1:] != v[:-1])
        self.G = csr_matrix((t[prem], (v[prem], u[prem])), shape=(n, n))
        self.n = n
        self.epsg = epsg
        self.vers_proj = Transformer.from_crs(4326, epsg, always_xy=True)
        self.x, self.y = self.vers_proj.transform(self.lon, self.lat)
        _, lab = connected_components(self.G, directed=True, connection="strong")
        ok = np.bincount(lab)[lab] >= TAILLE_MIN_COMPOSANTE
        self.accrochables = np.flatnonzero(ok)
        self.arbre = cKDTree(np.c_[self.x[ok], self.y[ok]])

    def accrocher_xy(self, x, y):
        """Nœud accrochable le plus proche + temps d'approche (s) depuis le point."""
        d, i = self.arbre.query(np.c_[np.atleast_1d(x), np.atleast_1d(y)], workers=-1)
        return self.accrochables[i].astype(np.int32), (d * DETOUR_APPROCHE / VITESSE_APPROCHE_MS).astype(np.float32), d

    def accrocher(self, lon, lat):
        x, y = self.vers_proj.transform(np.atleast_1d(lon), np.atleast_1d(lat))
        return self.accrocher_xy(x, y)

    def temps_vers(self, noeuds, limite_s=np.inf):
        """Temps (s) de chaque nœud vers la destination la plus proche parmi `noeuds`,
        et l'indice (dans `noeuds`) de cette destination."""
        noeuds = np.asarray(noeuds, np.int32)
        dist, _, src = dijkstra(self.G, directed=True, indices=noeuds, min_only=True,
                                limit=limite_s, return_predecessors=True)
        lut = np.full(self.n, -1, np.int32)
        lut[noeuds[::-1]] = np.arange(len(noeuds), dtype=np.int32)[::-1]
        rang = np.where(src >= 0, lut[np.maximum(src, 0)], -1).astype(np.int32)
        return dist.astype(np.float32), rang

    def temps_depuis_un(self, noeud, limite_s):
        """Dijkstra borné depuis un seul nœud (simulation d'un nouvel hôpital)."""
        return dijkstra(self.G, directed=True, indices=int(noeud), limit=limite_s).astype(np.float32)
