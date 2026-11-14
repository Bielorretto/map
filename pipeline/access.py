"""Étape 4 — Temps d'accès de base : un Dijkstra multi-sources par territoire.

Produit data/build/modele_<T>.npz (accrochage + temps) utilisé par l'API,
et affiche un résumé (population à plus de 30 min, temps moyens).
Usage : python access.py [FR GP MQ GF RE YT]
"""
import json
import sys
import time
from pathlib import Path

import numpy as np
import shapely

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.territoire import Territoire  # noqa: E402
from config import BUILD, RAW, TERRITOIRES  # noqa: E402

TESTS = {"CORSE": {"nom": "Corse (test)", "epsg": 3035, "res_m": 400, "deps": ["2A", "2B"], "pop": "FR"}}


def main(codes):
    meta = json.loads((BUILD / "departements.json").read_text())
    geoms = [shapely.geometry.shape(f["geometry"]) for f in json.load(open(RAW / "departements.geojson"))["features"]]
    sites = json.loads((BUILD / "sites.json").read_text())
    for code in codes:
        cfg = TERRITOIRES.get(code) or TESTS[code]
        t0 = time.time()
        T = Territoire.construire(code, cfg, meta, geoms, sites)
        m, pop = T.min_cel0, T.cel_pop
        fin = np.isfinite(m)
        print(f"{code} : {len(T.destinations)} services d'urgences, {len(pop):,} points de population, "
              f"{T.reseau.n:,} nœuds ({time.time() - t0:.0f}s)")
        print(f"   temps moyen {np.average(np.minimum(m, 120), weights=pop):.1f} min · "
              f"> 30 min : {pop[m >= 30].sum():,.0f} hab ({pop[m >= 30].sum() / pop.sum():.1%}) · "
              f"sans accès routier : {pop[~fin].sum():,.0f} hab")


if __name__ == "__main__":
    main(sys.argv[1:] or list(TERRITOIRES))
