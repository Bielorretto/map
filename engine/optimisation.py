"""Optimisation de l'implantation de nouveaux services d'urgences.

Deux modèles classiques de localisation-couverture :

* MCLP — Maximal Covering Location Problem (Church & ReVelle, 1974) :
  « où placer p nouveaux sites pour ramener le plus d'habitants sous le seuil ? »
      max  Σ_i w_i y_i
      s.c. y_i ≤ Σ_{j ∈ N_i} x_j     (i couvert seulement si un site j le couvre)
           Σ_j x_j = p,  x_j ∈ {0,1}, 0 ≤ y_i ≤ 1

* LSCP — Location Set Covering Problem (Toregas et al., 1971) :
  « combien de sites, au minimum, pour que (presque) tout le monde soit sous le seuil ? »
      min  Σ_j x_j
      s.c. Σ_i w_i y_i ≥ α · W_couvrable,  y_i ≤ Σ_{j ∈ N_i} x_j

N_i = candidats atteignables depuis le carreau i en moins du seuil, calculés
par Dijkstra élagué (engine/dijkstra.py) sur le réseau routier réel.
Résolution exacte par HiGHS (scipy.optimize.milp) ; glouton en secours.
"""
import time

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import csr_matrix, vstack, hstack, identity
from scipy.spatial import cKDTree

from .territoire import SEUIL_MIN, rassembler

MAILLE_CANDIDATS_M = 4000   # un candidat par maille peuplée (centre de gravité de la population)
RAYON_SCORE_M = 30000       # pré-sélection : population non couverte dans ce rayon


class Optimiseur:
    def __init__(self, T):
        self.T = T
        R = T.reseau
        self.cel_x, self.cel_y = R.vers_proj.transform(T.cel_lon.astype(np.float64), T.cel_lat.astype(np.float64))

    # ------------------------------------------------------------- préparation
    def _candidats(self, zone, loin, n_max):
        T = self.T
        idx = np.flatnonzero(zone & (T.cel_pop > 0) & T.ok_cel)
        gx = np.floor(self.cel_x[idx] / MAILLE_CANDIDATS_M).astype(np.int64)
        gy = np.floor(self.cel_y[idx] / MAILLE_CANDIDATS_M).astype(np.int64)
        _, g = np.unique(gx * 10_000_000 + gy, return_inverse=True)
        w = T.cel_pop[idx].astype(np.float64)
        sw = np.bincount(g, w)
        cx = np.bincount(g, w * self.cel_x[idx]) / sw
        cy = np.bincount(g, w * self.cel_y[idx]) / sw
        # Score : population encore au-delà du seuil dans un rayon de 30 km.
        il = np.flatnonzero(zone & loin & (T.cel_pop > 0))
        if not len(il):
            return np.array([], np.int32), np.zeros((0, 2))
        arbre = cKDTree(np.c_[self.cel_x[il], self.cel_y[il]])
        voisins = arbre.query_ball_point(np.c_[cx, cy], RAYON_SCORE_M, workers=-1)
        score = np.array([T.cel_pop[il[v]].sum() if v else 0.0 for v in voisins])
        ordre = np.argsort(-score)[:n_max]
        ordre = ordre[score[ordre] > 0]
        nd, _, _ = T.reseau.accrocher_xy(cx[ordre], cy[ordre])
        nd, u = np.unique(nd, return_index=True)
        return nd, np.c_[cx[ordre][u], cy[ordre][u]]

    def _couverture(self, cand, dem, seuil):
        """Matrice creuse demande × candidats : 1 si trajet < seuil (Dijkstra élagué)."""
        T = self.T
        lim = seuil * 60.0
        pos = np.full(len(T.cel_pop), -1, np.int64)
        pos[dem] = np.arange(len(dem))
        lignes, cols = [], []
        for j, c in enumerate(cand):
            noeuds, t = T.elagueur.ameliores(int(c), 0.0, lim)
            cel, tn = rassembler(T.idx_cel, noeuds, t)
            k = pos[cel]
            ok = (k >= 0) & (tn + T.cel_appr[cel] < lim)
            lignes.append(k[ok])
            cols.append(np.full(int(ok.sum()), j))
        li = np.concatenate(lignes) if lignes else np.array([], int)
        co = np.concatenate(cols) if cols else np.array([], int)
        return csr_matrix((np.ones(len(li), bool), (li, co)), shape=(len(dem), len(cand)))

    @staticmethod
    def _regrouper(A, w):
        """Fusionne les carreaux couverts exactement par les mêmes candidats."""
        A = A.tocsr()
        cles = {}
        groupe = np.empty(A.shape[0], np.int64)
        for i in range(A.shape[0]):
            k = A.indices[A.indptr[i]:A.indptr[i + 1]].tobytes()
            groupe[i] = cles.setdefault(k, len(cles))
        Wg = np.bincount(groupe, w, len(cles))
        rep = np.zeros(len(cles), np.int64)
        rep[groupe] = np.arange(A.shape[0])
        return A[rep], Wg

    # --------------------------------------------------------------- modèles
    def preparer(self, zone, minutes, seuil=SEUIL_MIN, n_max=220):
        t0 = time.time()
        T = self.T
        loin = ~(minutes < seuil)
        dem = np.flatnonzero(zone & loin & (T.cel_pop > 0) & T.ok_cel)
        if not len(dem):
            return None
        cand, cxy = self._candidats(zone, loin, n_max)
        if not len(cand):
            return None
        A = self._couverture(cand, dem, seuil)
        w = T.cel_pop[dem].astype(np.float64)
        couvrable = np.asarray(A.sum(1)).ravel() > 0
        Ag, Wg = self._regrouper(A[couvrable], w[couvrable])
        return {"cand": cand, "cxy": cxy, "A": A, "dem": dem, "w": w, "Ag": Ag, "Wg": Wg,
                "pop_loin": float(w.sum()), "pop_couvrable": float(w[couvrable].sum()),
                "t_prep": time.time() - t0, "seuil": seuil}

    @staticmethod
    def glouton(P, p):
        A, w = P["Ag"].tocsc(), P["Wg"].copy()
        choix, gains = [], []
        couvert = np.zeros(A.shape[0], bool)
        for _ in range(p):
            g = A.T @ (w * ~couvert)
            g[choix] = -1
            j = int(np.argmax(g))
            if g[j] <= 0:
                break
            choix.append(j)
            gains.append(float(g[j]))
            couvert |= A[:, j].toarray().ravel()
        return choix, gains

    @staticmethod
    def mclp(P, p, temps_max=20):
        A, w = P["Ag"].tocsr().astype(np.float64), P["Wg"]
        m, n = A.shape
        if m == 0:
            return [], "vide"
        c = np.concatenate([np.zeros(n), -w])  # variables : x (n) puis y (m)
        cons = [LinearConstraint(hstack([-A, identity(m)]), -np.inf, 0),
                LinearConstraint(np.concatenate([np.ones(n), np.zeros(m)])[None, :], p, p)]
        integ = np.concatenate([np.ones(n), np.zeros(m)])
        r = milp(c, constraints=cons, integrality=integ, bounds=Bounds(0, 1),
                 options={"time_limit": temps_max, "mip_rel_gap": 1e-4})
        if r.x is None:
            return None, r.message
        return list(np.flatnonzero(r.x[:n] > 0.5)), ("optimal" if r.status == 0 else "limite de temps")

    @staticmethod
    def lscp(P, alpha=1.0, temps_max=30):
        A, w = P["Ag"].tocsr().astype(np.float64), P["Wg"]
        m, n = A.shape
        if m == 0:
            return [], "vide"
        c = np.concatenate([np.ones(n), np.zeros(m)])
        cible = alpha * w.sum() - 1e-6
        cons = [LinearConstraint(hstack([-A, identity(m)]), -np.inf, 0),
                LinearConstraint(np.concatenate([np.zeros(n), w])[None, :], cible, np.inf)]
        integ = np.concatenate([np.ones(n), np.zeros(m)])
        r = milp(c, constraints=cons, integrality=integ, bounds=Bounds(0, 1),
                 options={"time_limit": temps_max, "mip_rel_gap": 1e-4})
        if r.x is None:
            return None, r.message
        return list(np.flatnonzero(r.x[:n] > 0.5)), ("optimal" if r.status == 0 else "limite de temps")

    def ordonner(self, P, choix):
        """Classe les sites retenus par gain marginal décroissant (lecture plus claire)."""
        A, w = P["Ag"].tocsc(), P["Wg"]
        reste, ordre, gains = list(choix), [], []
        couvert = np.zeros(A.shape[0], bool)
        while reste:
            g = [float((w * (A[:, j].toarray().ravel() & ~couvert)).sum()) for j in reste]
            k = int(np.argmax(g))
            j = reste.pop(k)
            ordre.append(j)
            gains.append(g[k])
            couvert |= A[:, j].toarray().ravel()
        return ordre, gains

    def sites(self, P, ordre, gains):
        R = self.T.reseau
        out = []
        for j, g in zip(ordre, gains):
            nd = int(P["cand"][j])
            out.append({"lon": float(R.lon[nd]), "lat": float(R.lat[nd]), "gain": round(g)})
        return out
