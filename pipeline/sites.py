"""Étape 1 — Sites d'urgences et SMUR réels, à partir de FINESS (data.gouv.fr).

Sources (Ministère de la Santé, extraction FINESS) :
  * autorisations d'activités de soins, ancien format (etalab-cs1100503) ;
  * autorisations au nouveau format AMM issues de la réforme 2023 (flux ARHGOS) ;
  * établissements géolocalisés (etalab-cs1100507), coordonnées Lambert/UTM.

Règle, par famille d'activité (urgences / SMUR / SAMU) :
  1. nouveau format AMM s'il existe pour cet établissement ;
  2. sinon ancien format courant (autorisations prorogées par la réforme) ;
  3. sinon stock historique FINESS au 31/12/2025 : pendant la transition, certaines
     autorisations prorogées (ex. CH de Rodez) ont quitté l'extraction courante sans
     être encore publiées au format AMM.
"""
import csv
import io
import json
import xml.etree.ElementTree as ET
from collections import defaultdict

from pyproj import Transformer

from config import BUILD, FINESS_DOM, RAW

# Ancien format : activité 14 = Médecine d'urgence.
ANCIEN = {"23": "SU", "24": "SUP", "26": "SMUR", "27": "SMURP", "29": "ASMUR", "22": "SAMU"}


def nouveau_type(modalite: str, mention: str) -> str | None:
    if modalite == "MO032":
        return "SUP" if mention == "ME064" else "SU"
    if modalite == "MO033":
        return "AMU"  # antenne de médecine d'urgence (horaires restreints)
    if modalite == "MO031":
        return {"ME064": "SMURP", "ME067": "ASMUR"}.get(mention, "SMUR")
    if modalite == "MO030":
        return "SAMU"
    return None


def lire_ancien():
    out = defaultdict(lambda: {"types": set(), "saison": False})
    with open(RAW / "finess_autorisations.csv", encoding="utf-8-sig") as f:
        for r in csv.reader(f, delimiter=";"):
            if r[0] != "activiteoffresoin" or r[3] != "14":
                continue
            typ = ANCIEN.get(r[5])
            if not typ or not r[19]:  # pas de date de mise en œuvre → pas en service
                continue
            e = out[r[11]]
            e["types"].add(typ)
            e["saison"] |= r[7] == "13"
            e["ej"] = r[1]
    return out


def lire_historique(fichier="etalab_stock_as_20251231.csv"):
    out = defaultdict(lambda: {"types": set(), "saison": False})
    chemin = RAW / "as_hist" / "etalab-stock-as-historique-2004-2025" / fichier
    if not chemin.exists():
        return out
    with open(chemin, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f, delimiter=";"):
            typ = ANCIEN.get(r["modalite"]) if r["activite"] == "14" else None
            if typ and r["datemeo"]:
                e = out[r["nofinesset"]]
                e["types"].add(typ)
                e["saison"] |= r["forme"] == "13"
                e["ej"] = r["nofinessej"]
    return out


def lire_amm():
    out = defaultdict(lambda: {"types": set(), "saison": False})
    for _, el in ET.iterparse(RAW / "amm.xml"):
        if el.tag != "activiteamm":
            continue
        if el.findtext("activite") == "QA014" and el.findtext("statut_autorisation_lib") == "Active" \
                and el.findtext("datemeo"):
            typ = nouveau_type(el.findtext("modalite"), el.findtext("mention"))
            if typ:
                e = out[el.findtext("nofinesset")]
                e["types"].add(typ)
                e["saison"] |= el.findtext("declaration") == "DE017"
                e["ej"] = el.findtext("nofinessej")
        el.clear()
    return out


def lire_etablissements():
    etab, geo = {}, {}
    with open(RAW / "finess_etab_geo.csv", encoding="utf-8-sig") as f:
        for r in csv.reader(f, delimiter=";"):
            r = [x.replace("\x92", "’") for x in r]  # apostrophe Windows-1252 restée dans la source
            if r[0] == "structureet":
                dep = FINESS_DOM.get(r[13], r[13])
                com = (dep[:2] + r[12]) if dep.startswith("97") else (dep + r[12])
                etab[r[1]] = {"nom": r[3].strip(), "nom_long": r[4].strip(), "dep": dep, "commune": com,
                              "adresse": " ".join(x for x in (r[7], r[8], r[9]) if x.strip()),
                              "ville": r[15][6:].strip().title(), "categorie": r[19], "statut": r[27]}
            elif r[0] == "geolocalisation" and r[2] and r[3]:
                epsg = r[4].split("EPSG:")[1].split()[0] if "EPSG:" in r[4] else None
                if "UTM zone 21N" in r[4]:
                    epsg = "32621"
                elif "UTM zone 1S" in r[4]:
                    epsg = "32701"
                if epsg:
                    # 1er champ = niveau de précision : 1 numéro, 2 rue, 3-4 centre de commune.
                    geo[r[1]] = (float(r[2]), float(r[3]), int(epsg), int(r[4].split(",")[0] or 9))
    return etab, geo


def positions_osm():
    """Hôpitaux OSM portant un numéro FINESS (tag ref:FR:FINESS) → position du bâtiment/emprise."""
    import shapely
    chemin = BUILD / "hopitaux-osm.geojsonseq"
    out = {}
    if not chemin.exists():
        return out
    for ligne in open(chemin, encoding="utf-8"):
        f = json.loads(ligne.lstrip("\x1e"))
        ref = (f["properties"] or {}).get("ref:FR:FINESS")
        if not ref:
            continue
        g = shapely.geometry.shape(f["geometry"])
        if g.geom_type in ("LineString", "MultiLineString"):
            continue
        pt = g.representative_point() if g.geom_type != "Point" else g
        aire = g.area
        for num in ref.replace(",", ";").split(";"):
            num = num.strip()
            if len(num) == 9 and (num not in out or aire > out[num][2]):
                out[num] = (round(pt.x, 6), round(pt.y, 6), aire)
    return out


def geocoder_ban(adresse, commune):
    """Géocodage d'une adresse par la Base Adresse Nationale (api-adresse.data.gouv.fr)."""
    import urllib.parse
    import urllib.request
    if not adresse.strip():
        return None
    url = ("https://api-adresse.data.gouv.fr/search/?limit=1&q=" + urllib.parse.quote(adresse)
           + "&citycode=" + commune)
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            f = json.load(r)["features"]
    except Exception:  # noqa: BLE001
        return None
    if f and f[0]["properties"]["score"] >= 0.5 and f[0]["properties"]["type"] in ("housenumber", "street"):
        return tuple(f[0]["geometry"]["coordinates"])
    return None


def main():
    ancien, amm, hist = lire_ancien(), lire_amm(), lire_historique()
    etab, geo = lire_etablissements()
    osm = positions_osm()
    corriges = 0
    trans = {}
    sites = []
    # Familles : une antenne (AMU) remplace un SU, mais des urgences pédiatriques (SUP)
    # ne remplacent pas des urgences adultes (cas du CH de Perpignan).
    familles = {"SU": "SU", "AMU": "SU", "SUP": "SUP", "SMUR": "SMUR", "ASMUR": "SMUR", "SMURP": "SMURP",
                "SAMU": "SAMU"}
    vide = {"types": set(), "saison": False}
    complements = 0
    for fin in sorted(set(ancien) | set(amm) | set(hist)):
        # Par famille d'activité : AMM, sinon ancien format courant, sinon stock 31/12/2025.
        n, a, h = amm.get(fin, vide), ancien.get(fin, vide), hist.get(fin, vide)
        types = set(n["types"])
        for source in (a, h):
            deja = {familles[t] for t in types}
            ajout = {t for t in source["types"] if familles[t] not in deja}
            if source is h and ajout & {"SU", "AMU", "SMUR", "ASMUR"}:
                complements += 1
            types |= ajout
        if not types:
            continue
        src = {"types": types, "saison": a["saison"] or n["saison"], "ej": n.get("ej") or a.get("ej") or h.get("ej")}
        if fin not in etab or fin not in geo:
            print("  ! sans géolocalisation :", fin)
            continue
        x, y, epsg, prec = geo[fin]
        if epsg not in trans:
            trans[epsg] = Transformer.from_crs(epsg, 4326, always_xy=True)
        lon, lat = trans[epsg].transform(x, y)
        position = "adresse FINESS" if prec <= 2 else "centre de commune (FINESS)"
        # Position imprécise (centre de commune) : on prend l'emprise OSM de l'établissement.
        if prec >= 3 and fin in osm:
            lon, lat = osm[fin][0], osm[fin][1]
            position = "emprise OpenStreetMap (ref:FR:FINESS)"
            corriges += 1
        elif prec >= 3:
            ban = geocoder_ban(etab[fin]["adresse"], etab[fin]["commune"])
            if ban:
                lon, lat = ban
                position = "adresse géocodée (BAN)"
                corriges += 1
        e = etab[fin]
        sites.append({
            "finess": fin, "ej": src.get("ej"), "nom": e["nom"], "nom_long": e["nom_long"],
            "ville": e["ville"], "dep": e["dep"], "commune": e["commune"], "statut": e["statut"],
            "types": sorted(src["types"]), "saisonnier": src["saison"],
            "source": "AMM" if fin in amm else ("AS" if fin in ancien else "AS-2025"), "position": position,
            "lon": round(lon, 6), "lat": round(lat, 6),
        })
    BUILD.mkdir(parents=True, exist_ok=True)
    (BUILD / "sites.json").write_text(json.dumps(sites, ensure_ascii=False, indent=0))
    n = lambda t: sum(t in s["types"] for s in sites)
    print(f"  dont {complements} complétés par le stock FINESS au 31/12/2025")
    imprec = sum(s["position"].startswith("centre") for s in sites)
    print(f"  positions : {corriges} recalées (OSM ou BAN), {imprec} restent au centre de la commune")
    print(f"{len(sites)} établissements · SU {n('SU')} · antennes {n('AMU')} · SUP {n('SUP')} · "
          f"SMUR {n('SMUR')} · antennes SMUR {n('ASMUR')} · SAMU {n('SAMU')}")


if __name__ == "__main__":
    main()
