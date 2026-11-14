"""Paramètres communs du pipeline « Accès urgences »."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"          # fichiers sources téléchargés (non versionnés)
BUILD = ROOT / "data" / "build"      # résultats intermédiaires (graphes, temps)
WEB_DATA = ROOT / "web" / "data"     # données servies au front

SEUIL_MIN = 30  # seuil réglementaire d'accès aux soins urgents (minutes)

# Territoires couverts : extrait OSM Geofabrik + projection métrique locale.
# FINESS code les DROM 9A..9F ; on les convertit en codes INSEE 971..976.
TERRITOIRES = {
    "FR": {"nom": "France métropolitaine", "pbf": "france-latest.osm.pbf", "epsg": 3035, "res_m": 600},
    "GP": {"nom": "Guadeloupe", "pbf": "guadeloupe-latest.osm.pbf", "epsg": 5490, "res_m": 250, "dep": "971"},
    "MQ": {"nom": "Martinique", "pbf": "martinique-latest.osm.pbf", "epsg": 5490, "res_m": 250, "dep": "972"},
    "GF": {"nom": "Guyane", "pbf": "guyane-latest.osm.pbf", "epsg": 2972, "res_m": 1000, "dep": "973"},
    "RE": {"nom": "La Réunion", "pbf": "reunion-latest.osm.pbf", "epsg": 2975, "res_m": 250, "dep": "974"},
    "YT": {"nom": "Mayotte", "pbf": "mayotte-latest.osm.pbf", "epsg": 4471, "res_m": 250, "dep": "976"},
}
DEP_TO_TERR = {t["dep"]: k for k, t in TERRITOIRES.items() if "dep" in t}
FINESS_DOM = {"9A": "971", "9B": "972", "9C": "973", "9D": "974", "9E": "975", "9F": "976"}


def terr_of_dep(dep: str) -> str | None:
    if dep in DEP_TO_TERR:
        return DEP_TO_TERR[dep]
    return "FR" if dep and not dep.startswith(("97", "98")) else None


# --- Profil de vitesse voiture (km/h), repris du profil car.lua d'OSRM ---------
# Utilisé quand la voie n'a pas de tag maxspeed exploitable.
VITESSES = {
    "motorway": 90, "motorway_link": 45,
    "trunk": 85, "trunk_link": 40,
    "primary": 65, "primary_link": 30,
    "secondary": 55, "secondary_link": 25,
    "tertiary": 40, "tertiary_link": 20,
    "unclassified": 25, "residential": 25, "living_street": 10,
}
# Comme OSRM : la vitesse réellement pratiquée vaut ~80 % de la limite affichée.
REDUCTION_MAXSPEED = 0.8
# Vitesses implicites des tags maxspeed symboliques (Code de la route).
MAXSPEED_SYMBOLES = {
    "FR:urban": 50, "FR:rural": 80, "FR:motorway": 130, "FR:walk": 6,
    "FR:zone30": 30, "FR:zone:30": 30, "none": 130,
}
# Segmentation : on conserve un nœud au moins tous les X mètres le long d'une voie
# pour que l'accrochage (snapping) d'un carreau reste précis.
PAS_SEGMENT_M = 800.0
