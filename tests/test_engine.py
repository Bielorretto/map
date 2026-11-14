"""Tests du moteur sur le territoire de test « CORSE » (python pipeline/access.py CORSE)."""
import sys
from pathlib import Path

import numpy as np
import pytest
from scipy.sparse.csgraph import dijkstra

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))
from access import TESTS  # noqa: E402
from engine.optimisation import Optimiseur  # noqa: E402
from engine.simulation import Simulateur, statistiques  # noqa: E402
from engine.territoire import BUILD, Territoire  # noqa: E402

pytestmark = pytest.mark.skipif(not (BUILD / "modele_CORSE.npz").exists(), reason="modèle CORSE absent")


@pytest.fixture(scope="module")
def T():
    return Territoire.charger("CORSE", TESTS["CORSE"])


@pytest.mark.parametrize("lon,lat", [(9.149, 42.306), (8.95, 41.7), (9.45, 42.1), (8.75, 42.55)])
def test_dijkstra_elague_exact(T, lon, lat):
    """Le Dijkstra élagué trouve exactement les nœuds améliorés qu'un Dijkstra complet."""
    nd, ap, _ = T.reseau.accrocher(lon, lat)
    noeuds, t = T.elagueur.ameliores(nd[0], ap[0], 75 * 60)
    ref = dijkstra(T.reseau.G, indices=int(nd[0]), limit=75 * 60) + ap[0]
    attendu = np.flatnonzero((ref < np.minimum(T.T_noeud, 1e12)) & (ref <= 75 * 60))
    assert set(noeuds.tolist()) == set(attendu.tolist())
    assert np.allclose(t, ref[noeuds], atol=1e-2)
    assert np.isinf(T.elagueur.dist).all(), "tampons non remis à zéro"


def test_temps_de_base_plausibles(T):
    m = T.min_cel0
    assert np.nanmin(m) >= 0
    assert np.average(np.minimum(m, 120), weights=T.cel_pop) < 30  # temps moyen par habitant
    # À côté d'un service d'urgences, le temps est de quelques minutes.
    for s in T.destinations:
        nd, ap, _ = T.reseau.accrocher(s["lon"], s["lat"])
        assert T.T_noeud[nd[0]] / 60 < 3


def test_simulation_monotone(T):
    S = Simulateur(T)
    m1 = S.minutes([(9.149, 42.306)])
    m2 = S.minutes([(9.149, 42.306), (9.45, 42.1)])
    assert (m1 <= T.min_cel0 + 1e-6).all() and (m2 <= m1 + 1e-6).all()
    assert statistiques(T, m2)["pop_loin"] < statistiques(T, T.min_cel0)["pop_loin"]


def test_mclp_coherent(T):
    O = Optimiseur(T)
    P = O.preparer(np.ones(len(T.cel_pop), bool), T.min_cel0)
    g1, _ = O.glouton(P, 1)
    c1, st = O.mclp(P, 1)
    _, gains1 = O.ordonner(P, c1)
    assert st == "optimal" and abs(gains1[0] - P["Wg"] @ P["Ag"][:, g1[0]].toarray().ravel()) < 1
    c3, _ = O.mclp(P, 3)
    _, gains3 = O.ordonner(P, c3)
    gg, _ = O.glouton(P, 3)
    assert sum(gains3) >= sum(O.ordonner(P, gg)[1]) - 1  # l'optimum exact bat (ou égale) le glouton
    # Le gain annoncé correspond à une vraie simulation (à l'arrondi près).
    S = Simulateur(T)
    sites = [(s["lon"], s["lat"]) for s in O.sites(P, *O.ordonner(P, c3))]
    m = S.minutes(sites)
    reel = T.cel_pop[(T.min_cel0 >= 30) & (m < 30)].sum()
    assert reel >= sum(gains3) * 0.98


def test_lscp_couvre_tout(T):
    O = Optimiseur(T)
    P = O.preparer(np.ones(len(T.cel_pop), bool), T.min_cel0)
    c, st = O.lscp(P, 1.0)
    A = P["Ag"].tocsc()[:, c]
    assert (np.asarray(A.sum(1)).ravel() > 0).all()


def test_requetes_simultanees_identiques_au_sequentiel(T):
    """Plusieurs simulations en parallèle (assistant qui pose N hôpitaux, glisser pendant un calcul)
    donnent exactement les mêmes résultats qu'une à une, et ne modifient pas les temps de base."""
    from concurrent.futures import ThreadPoolExecutor
    base = T.min_cel0.copy(), T.T_noeud.copy()
    rng = np.random.default_rng(0)
    pts = list(zip(rng.uniform(8.7, 9.5, 24), rng.uniform(41.6, 42.9, 24)))
    ref = [Simulateur(T, 0).impact(x, y) for x, y in pts]
    for _ in range(3):
        with ThreadPoolExecutor(12) as ex:
            res = list(ex.map(lambda p: Simulateur(T, 0).impact(*p), pts))
        for a, b in zip(ref, res):
            assert (a is None) == (b is None)
            if a is not None:
                assert np.array_equal(np.sort(a["cel"]), np.sort(b["cel"]))
                assert np.isclose(a["min"].sum(), b["min"].sum())
    assert np.array_equal(base[0], T.min_cel0) and np.array_equal(base[1], T.T_noeud)
