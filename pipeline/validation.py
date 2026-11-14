"""Étape 5 — Validation des temps de trajet contre des moteurs de référence.

Tire au hasard N carreaux habités (pondérés par la population), calcule notre
temps vers le service d'urgences le plus proche, puis interroge pour le même
trajet :
  * OSRM (serveur de démonstration public, profil voiture) ;
  * l'API d'itinéraire IGN Géoplateforme (BD TOPO, ressource bdtopo-osrm).
Écrit data/build/validation_<T>.csv et affiche écart médian / moyen.

Usage : python validation.py FR 120   (1 requête/s : usage raisonnable des API publiques)
"""
import csv
import json
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import BUILD, TERRITOIRES  # noqa: E402
from engine.territoire import Territoire  # noqa: E402

UA = {"User-Agent": "acces-urgences-validation/1.0"}


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        return json.load(r)


def osrm(a, b):
    d = get(f"https://router.project-osrm.org/route/v1/driving/{a[0]},{a[1]};{b[0]},{b[1]}?overview=false")
    return d["routes"][0]["duration"] / 60


def ign(a, b):
    d = get("https://data.geopf.fr/navigation/itineraire?resource=bdtopo-osrm&profile=car&optimization=fastest"
            f"&start={a[0]},{a[1]}&end={b[0]},{b[1]}&getSteps=false&timeUnit=minute")
    return float(d["duration"])


def main(code, n):
    T = Territoire.charger(code, TERRITOIRES[code])
    rng = np.random.default_rng(42)
    ok = np.flatnonzero(T.ok_cel & np.isfinite(T.min_cel0) & (T.cel_pop > 0))
    tir = rng.choice(ok, size=min(n, len(ok)), replace=False, p=T.cel_pop[ok] / T.cel_pop[ok].sum())
    dest_idx = T.dest_ok[T.plus_proche_site(tir)]
    lignes = []
    for i, (c, s) in enumerate(zip(tir, dest_idx)):
        a = (round(float(T.cel_lon[c]), 5), round(float(T.cel_lat[c]), 5))
        h = T.destinations[s]
        b = (h["lon"], h["lat"])
        ligne = {"lon": a[0], "lat": a[1], "hopital": h["nom"], "nous_min": round(float(T.min_cel0[c]), 2)}
        for nom, f in (("osrm_min", osrm), ("ign_min", ign)):
            try:
                ligne[nom] = round(f(a, b), 2)
            except Exception as e:  # noqa: BLE001
                ligne[nom] = None
                print("  !", nom, e)
            time.sleep(1.0)
        lignes.append(ligne)
        print(f"{i + 1:3d}/{len(tir)} nous {ligne['nous_min']:5.1f} · OSRM {ligne['osrm_min']} · IGN {ligne['ign_min']}  ({h['nom']})")
    with open(BUILD / f"validation_{code}.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(lignes[0]))
        w.writeheader()
        w.writerows(lignes)
    nous = np.array([l["nous_min"] for l in lignes])
    for ref in ("osrm_min", "ign_min"):
        r = np.array([l[ref] if l[ref] is not None else np.nan for l in lignes], float)
        m = np.isfinite(r)
        e = nous[m] - r[m]
        print(f"{ref:9s} : n={m.sum()}  écart médian {np.median(e):+.1f} min · écart absolu moyen {np.abs(e).mean():.1f} min · "
              f"ratio médian {np.median(nous[m] / np.maximum(r[m], .5)):.2f} · corrélation {np.corrcoef(nous[m], r[m])[0, 1]:.3f}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "FR", int(sys.argv[2]) if len(sys.argv) > 2 else 100)
