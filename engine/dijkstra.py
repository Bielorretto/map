"""Dijkstra élagué (compilé avec numba) pour l'ajout d'un nouveau site.

Propriété exploitée : si, en atteignant un nœud v depuis le nouveau site, on a
d_new(v) ≥ T(v) (temps actuel vers le service existant le plus proche), alors
aucun nœud w dont le trajet passe par v ne peut être amélioré, car
T(w) ≤ t(w→v) + T(v) ≤ t(w→v) + d_new(v). On peut donc arrêter l'exploration
en v : le résultat est EXACT et l'exploration se limite à la zone améliorée,
quelques milliers de nœuds au lieu de millions.
Le même argument vaut pour la couverture d'un candidat (optimisation) : un
carreau encore au-delà du seuil n'est jamais atteint via un nœud élagué.
"""
import heapq
import threading

import numpy as np
from numba import njit


@njit(cache=True)
def _elague(indptr, indices, poids, src, t0, Tbase, limite, dist, sortie):
    n_out = 0
    tas = [(t0, src)]
    dist[src] = t0
    while tas:
        d, u = heapq.heappop(tas)
        if d > dist[u]:
            continue
        if d >= Tbase[u] or d > limite:
            continue
        sortie[n_out] = u
        n_out += 1
        for k in range(indptr[u], indptr[u + 1]):
            v = np.int64(indices[k])
            nd = d + poids[k]
            if nd < dist[v] and nd <= limite:
                dist[v] = nd
                heapq.heappush(tas, (nd, v))
    return n_out


class Elagueur:
    """Garde des tampons de taille n réutilisés d'un appel à l'autre."""

    def __init__(self, G, Tbase):
        # Pas de copie : numba lit directement les tableaux CSR de scipy.
        self.indptr, self.indices, self.poids = G.indptr, G.indices, G.data
        self.Tbase = np.where(np.isfinite(Tbase), Tbase, 1e12).astype(np.float64)
        n = G.shape[0]
        self.dist = np.full(n, np.inf)
        self.sortie = np.empty(n, np.int64)
        # Les tampons sont partagés : deux requêtes simultanées (glisser + assistant, ou plusieurs
        # hôpitaux posés d'un coup) se mélangeraient. Le verrou rend chaque calcul exclusif ;
        # numba garde le GIL pendant _elague, on ne perd donc pas de parallélisme réel.
        self.verrou = threading.Lock()

    def ameliores(self, src, t0, limite_s):
        """Nœuds améliorés par un site accroché en `src` (approche t0 s) et leur nouveau temps (s)."""
        with self.verrou:
            k = _elague(self.indptr, self.indices, self.poids, int(src), float(t0), self.Tbase,
                        float(limite_s), self.dist, self.sortie)
            noeuds = self.sortie[:k].copy()
            temps = self.dist[noeuds].astype(np.float32)
            self.dist[int(src)] = np.inf
            self._reset_local(noeuds)
        return noeuds, temps

    def _reset_local(self, noeuds):
        """Remet à l'infini les nœuds explorés et leurs voisins (seuls nœuds touchés)."""
        self.dist[noeuds] = np.inf
        ip, ind = self.indptr, self.indices
        if len(noeuds):
            starts, ends = ip[noeuds], ip[noeuds + 1]
            tot = int((ends - starts).sum())
            if tot:
                idx = np.repeat(starts - np.cumsum(np.r_[0, (ends - starts)[:-1]]), ends - starts) + np.arange(tot)
                self.dist[ind[idx]] = np.inf
