"""Assistant « cœur » : Claude répond aux questions en appelant le moteur de calcul.

Le modèle ne calcule rien lui-même : chaque chiffre qu'il donne vient d'un des
outils ci-dessous (classement, statistiques, simulation, optimisation MCLP /
LSCP, recherche de lieu ou d'établissement FINESS). Les outils renvoient aussi
des « actions » que l'interface applique aussitôt sur la carte (poser des
hôpitaux, isoler une région, ouvrir le classement…).

Mémoire : à chaque question, l'interface renvoie l'historique (texte seul) et
l'état de la carte (hôpitaux simulés, zone affichée). Le serveur ne garde rien.
"""
import json
import os
import re
import unicodedata
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

from engine.simulation import LIMITE_MIN, statistiques

# Fournisseur du LLM : OpenAI si OPENAI_API_KEY est fournie (défaut), sinon Claude (Anthropic).
FOURNISSEUR = (os.environ.get("ASSISTANT_FOURNISSEUR")
               or ("openai" if os.environ.get("OPENAI_API_KEY") else "anthropic")).strip().lower()
MODELE = os.environ.get("ASSISTANT_MODELE") or ("gpt-6-astra" if FOURNISSEUR == "openai" else "claude-opus-5-5")
EFFORT = os.environ.get("ASSISTANT_EFFORT") or ("high" if FOURNISSEUR == "openai" else "medium")
MAX_APPELS = 8        # allers-retours modèle ↔ outils pour une question
MAX_HISTORIQUE = 16   # messages texte gardés comme mémoire de conversation

TYPES = {"SU": "structure des urgences", "AMU": "antenne de médecine d'urgence", "SUP": "urgences pédiatriques",
         "SMUR": "SMUR", "SMURP": "SMUR pédiatrique", "ASMUR": "antenne SMUR", "SAMU": "SAMU"}
DROM = {"971", "972", "973", "974", "976"}
ALIAS = {"paca": "provence alpes cote d azur", "sud": "provence alpes cote d azur", "idf": "ile de france",
         "region parisienne": "ile de france", "aura": "auvergne rhone alpes", "bfc": "bourgogne franche comte",
         "reunion": "la reunion", "centre": "centre val de loire", "corse du sud": "corse du sud"}


SOURCES = {
    "FINESS": {"court": "FINESS",
               "label": "FINESS : autorisations « médecine d’urgence » et établissements géolocalisés, Ministère de la "
                        "Santé (extraction de mai 2026, data.gouv.fr)",
               "url": "https://www.data.gouv.fr/fr/datasets/finess-extraction-des-autorisations-dactivites-de-soin/"},
    "INSEE": {"court": "INSEE 2021",
              "label": "INSEE : Filosofi 2021, population en carreaux de 200 m (métropole) ; populations légales "
                       "communales (DROM)",
              "url": "https://www.data.gouv.fr/fr/datasets/revenus-pauvrete-et-niveau-de-vie-donnees-carroyees-dispositif-fichier-localise-social-et-fiscal-filosofi/"},
    "OSM": {"court": "OpenStreetMap",
            "label": "Réseau routier OpenStreetMap (Geofabrik) ; temps calculés par Dijkstra, vitesses du profil "
                     "voiture d’OSRM, sans trafic (écart moyen de 0,7 min avec OSRM)",
            "url": "https://download.geofabrik.de/europe/france.html"},
    "OPTIM": {"court": "MCLP / LSCP",
              "label": "Optimisation exacte (programmation linéaire en nombres entiers, HiGHS) : MCLP (Church & "
                       "ReVelle, 1974) et LSCP (Toregas, 1971), sur les temps de route OSM",
              "url": None},
    "COMMUNES": {"court": "geo.api.gouv.fr",
                 "label": "Communes et emplacement des mairies : geo.api.gouv.fr (code officiel géographique INSEE)",
                 "url": "https://geo.api.gouv.fr/decoupage-administratif/communes"},
}
SOURCES_OUTIL = {
    "classement_departements": ["INSEE", "OSM", "FINESS"],
    "statistiques_zone": ["INSEE", "OSM", "FINESS"],
    "chercher_lieu": ["COMMUNES", "OSM", "FINESS"],
    "simuler_hopitaux": ["INSEE", "OSM", "COMMUNES"],
    "optimiser_implantations": ["OPTIM", "INSEE", "OSM"],
    "combien_hopitaux": ["OPTIM", "INSEE", "OSM"],
    "retirer_hopitaux": [],
    "chercher_etablissement": ["FINESS"],
}


class Erreur(Exception):
    """Erreur d'utilisation d'un outil, renvoyée au modèle (is_error)."""


def norm(s):
    s = unicodedata.normalize("NFD", str(s).lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[’'`\-_/.,;:()«»\"]", " ", s)
    s = re.sub(r"\bst\b", "saint", s)
    s = re.sub(r"\bste\b", "sainte", s)
    return " ".join(s.split())


def sans_article(n):
    """« le département de la Lozère » → « lozere » ; « région Occitanie » → « occitanie »."""
    mots = n.split()
    vides = {"le", "la", "les", "l", "du", "de", "des", "d", "en", "dans", "departement", "region", "territoire"}
    while mots and mots[0] in vides:
        mots.pop(0)
    return " ".join(mots)


# ----------------------------------------------------------------------------- outils (schémas)
def _outil(nom, description, proprietes, requis=()):
    return {"name": nom, "description": description, "eager_input_streaming": True,
            "input_schema": {"type": "object", "properties": proprietes, "required": list(requis)}}


ZONES = {"type": "array", "items": {"type": "string"},
         "description": "Départements (nom ou code : « Lozère », « 48 », « 2A », « 971 »), régions (« Occitanie »), "
                        "« France », « métropole » ou « outre-mer ». Une zone d'optimisation doit rester dans un "
                        "seul territoire (métropole, ou un DROM)."}
REMPLACER = {"type": "boolean",
             "description": "true pour retirer d'abord tous les hôpitaux déjà simulés (« au lieu de », « plutôt », "
                            "« à la place »). Par défaut false : les hôpitaux déjà posés sont conservés."}

OUTILS = [
    _outil("classement_departements",
           "Classe les départements (et DROM) selon l'accès aux urgences, avec les hôpitaux simulés actuellement "
           "sur la carte. À appeler pour toute question de priorités, de « pires » zones, de déserts médicaux "
           "d'urgence ou de comparaison entre départements. Ouvre le classement sur la carte quand le périmètre "
           "est la France entière.",
           {"perimetre": {"type": "string", "description": "« France » (défaut), « métropole », « outre-mer » ou "
                                                            "une région."},
            "critere": {"type": "string", "enum": ["habitants_au_dela", "part_au_dela", "temps_moyen"],
                        "description": "habitants_au_dela = nombre d'habitants à 30 min ou plus (défaut) ; "
                                       "part_au_dela = pourcentage ; temps_moyen = temps moyen pondéré."},
            "nombre": {"type": "integer", "description": "Nombre de lignes (1 à 30, défaut 10)."}}),
    _outil("statistiques_zone",
           "Indicateurs d'une zone avec les hôpitaux simulés actuels : habitants, habitants à 30 min ou plus, "
           "temps moyen, nombre de services d'urgences, détail par département (et communes les plus éloignées "
           "en outre-mer). Isole la zone sur la carte.",
           {"zones": ZONES, "afficher": {"type": "boolean", "description": "Isoler la zone sur la carte (défaut true)."}},
           ["zones"]),
    _outil("chercher_lieu",
           "Trouve une commune et donne son temps de route actuel jusqu'aux urgences et le service le plus "
           "proche. À appeler pour « combien de temps depuis X », « hôpital le plus proche de X », ou pour "
           "situer un lieu.",
           {"nom": {"type": "string", "description": "Nom de la commune."},
            "departement": {"type": "string", "description": "Code ou nom du département pour lever une ambiguïté "
                                                             "(optionnel)."}},
           ["nom"]),
    _outil("simuler_hopitaux",
           "Pose un ou plusieurs nouveaux services d'urgences à des endroits précis (communes ou coordonnées) "
           "et calcule l'effet exact sur le réseau routier : habitants ramenés sous 30 min, départements "
           "touchés. Les hôpitaux apparaissent sur la carte. À appeler pour « et si on ouvrait des urgences à X ».",
           {"lieux": {"type": "array", "description": "Emplacements à simuler.",
                      "items": {"type": "object", "properties": {
                          "commune": {"type": "string"}, "departement": {"type": "string"},
                          "lon": {"type": "number"}, "lat": {"type": "number"}}}},
            "remplacer": REMPLACER},
           ["lieux"]),
    _outil("optimiser_implantations",
           "Cherche où placer un nombre donné de nouveaux services d'urgences dans une zone pour ramener le "
           "plus d'habitants possible sous 30 min (MCLP, résolu exactement). Les hôpitaux sont posés sur la "
           "carte. À appeler pour « où placer N hôpitaux », « meilleur emplacement ».",
           {"zones": ZONES, "nombre": {"type": "integer", "description": "Nombre d'hôpitaux (1 à 15)."},
            "remplacer": REMPLACER},
           ["zones", "nombre"]),
    _outil("combien_hopitaux",
           "Calcule le nombre minimal de nouveaux services d'urgences pour que toute la population atteignable "
           "d'une zone passe sous 30 min (LSCP, résolu exactement), et où les mettre. À appeler pour « combien "
           "d'hôpitaux pour mettre X au vert ».",
           {"zones": ZONES,
            "part_population": {"type": "number", "description": "Part de la population couvrable à ramener sous "
                                                                 "30 min, de 0.5 à 1 (défaut 1 = toute)."},
            "placer": {"type": "boolean", "description": "Poser les hôpitaux trouvés sur la carte (défaut true)."},
            "remplacer": REMPLACER},
           ["zones"]),
    _outil("retirer_hopitaux",
           "Retire des hôpitaux simulés de la carte, par leur numéro dans « État de la carte ». Liste vide = "
           "tous.",
           {"numeros": {"type": "array", "items": {"type": "integer"}}}),
    _outil("chercher_etablissement",
           "Cherche un établissement dans FINESS (urgences, SMUR, SAMU) par nom, ville ou numéro FINESS, et le "
           "montre sur la carte.",
           {"recherche": {"type": "string"}}, ["recherche"]),
]

ETAPES = {
    "classement_departements": "Classement des départements",
    "statistiques_zone": "Indicateurs de la zone",
    "chercher_lieu": "Temps d’accès depuis le lieu",
    "simuler_hopitaux": "Simulation sur le réseau routier",
    "optimiser_implantations": "Recherche des meilleurs emplacements",
    "combien_hopitaux": "Calcul du nombre minimal d’hôpitaux",
    "retirer_hopitaux": "Retrait des hôpitaux simulés",
    "chercher_etablissement": "Recherche dans FINESS",
}

SYSTEME = """Tu es l'assistant de « Accès Urgences », un outil d'aide à la décision qui mesure le temps de route entre chaque habitant et le service d'urgences le plus proche (France métropolitaine et DROM), et qui simule ou optimise l'implantation de nouveaux services.

Données : FINESS (autorisations « médecine d'urgence » : structures des urgences et antennes, SMUR) ; INSEE Filosofi 2021 (carreaux de 200 m) en métropole ; populations légales communales dans les DROM ; réseau routier OpenStreetMap. Les temps sont calculés par Dijkstra sur le graphe routier (vitesses du profil voiture d'OSRM, sans trafic), validés contre OSRM (écart moyen 0,7 min). Seuil de référence : {seuil} minutes.

Règles :
- Tout chiffre que tu donnes (habitants, minutes, nombre d'hôpitaux, gains) vient d'un outil appelé dans cette conversation. N'estime jamais de tête et n'invente rien. Si aucun outil ne permet de répondre, dis-le.
- Appelle les outils sans demander la permission : la personne attend que la carte bouge. Les hôpitaux que tu poses sont des hôpitaux simulés ordinaires : on peut les déplacer à la main ou les retirer (bouton « Tout retirer »).
- « Au vert » ou « sous 30 min » signifie un temps de route inférieur au seuil. « Combien d'hôpitaux pour mettre X au vert » → combien_hopitaux. « Où placer N hôpitaux » → optimiser_implantations. « Et si on ouvrait des urgences à tel endroit » → simuler_hopitaux.
- « Et si on en met 2 au lieu de 3 », « plutôt », « à la place » → remplacer=true, pour retirer les hôpitaux simulés précédents.
- Les emplacements trouvés par l'optimisation sont des points du réseau routier au cœur de bassins de population (candidats sur une grille de 4 km), pas des adresses : présente-les par la commune la plus proche.
- Réponds en français en 1 ou 2 phrases courtes (35 mots au plus). Les détails (classements, emplacements, chiffres par département, temps) s'affichent automatiquement sous ta réponse en graphiques : ne les recopie pas, donne l'essentiel. Un ou deux chiffres clés en **gras**, arrondis (12 400 habitants, 1,2 million, 18 min). Pas de liste, de tableau ni de titre, sauf si la personne demande le détail.
- Source chaque affirmation : juste après chaque chiffre ou fait, mets entre crochets l'étiquette de sa source, prise dans le champ « sources » du résultat d'outil : [FINESS], [INSEE], [OSM], [OPTIM] ou [COMMUNES] ; plusieurs à la fois : [INSEE·OSM]. N'écris aucune affirmation qui ne vient pas d'un résultat d'outil.
- Dis en quelques mots ce qui a changé sur la carte (« 3 hôpitaux posés sur la carte »).
- Les hôpitaux que tu poses sont des simulations affichées sur la carte, jamais ajoutées aux données FINESS. Quand des hôpitaux simulés sont présents, les indicateurs les incluent : précise-le (« avec les 2 hôpitaux simulés »), et rappelle que « Tout retirer » ramène à la situation réelle.
- Ne rappelle une limite que si elle compte pour la réponse : temps sans trafic ; liaisons maritimes non modélisées (Petite-Terre à Mayotte, îles sans service) ; population des DROM comptée à la mairie de chaque commune ; collectivités d'outre-mer (Saint-Martin, Polynésie…) non couvertes ; urgences uniquement pédiatriques non comptées.
- Hors du sujet (accès aux urgences, offre de soins d'urgence, implantations) : décline poliment en une phrase et propose une question utile.
- Chaque message de la personne commence par un bloc « État de la carte » qui liste les hôpitaux simulés (numérotés) et la zone affichée. Sers-toi de ces numéros avec retirer_hopitaux."""


# ----------------------------------------------------------------------------- outils (exécution)
def _texte(inp, k, defaut=None):
    v = inp.get(k, defaut)
    if v is None:
        return defaut
    if not isinstance(v, str):
        raise Erreur(f"« {k} » doit être un texte")
    return v.strip()


def _entier(inp, k, lo, hi, defaut):
    v = inp.get(k, defaut)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise Erreur(f"« {k} » doit être un nombre entier")
    return int(max(lo, min(hi, round(v))))


def _bool(inp, k, defaut):
    v = inp.get(k, defaut)
    return v if isinstance(v, bool) else defaut


def _liste_textes(inp, k):
    v = inp.get(k)
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not v or not all(isinstance(x, str) and x.strip() for x in v):
        raise Erreur(f"« {k} » doit être une liste de noms de zones")
    return v


class Contexte:
    """État d'une question : hôpitaux simulés (synchronisés avec la carte) et caches de calcul."""

    def __init__(self, sims, region, terr):
        self.sims, self.region, self.terr = sims, region, terr
        self.minutes, self.stats = {}, {}


class Assistant:
    def __init__(self, T, SIM, DEPS, territoire_du_point, optimisation, raw, build, seuil):
        self.T, self.SIM, self.DEPS, self.tdp, self.optimisation, self.S = T, SIM, DEPS, territoire_du_point, optimisation, seuil
        self.terr_de = {int(i): c for c, t in T.items() for i in t.deps}          # indice DEPS → territoire
        self.idx_code = {d["code"]: i for i, d in enumerate(DEPS)}
        self.regions = {}
        for i in self.terr_de:
            self.regions.setdefault(DEPS[i]["region_nom"], []).append(i)
        self.n_deps = {norm(DEPS[i]["nom"]): i for i in self.terr_de}
        self.n_regs = {norm(r): r for r in self.regions}
        # communes (geo.api.gouv.fr) : géocodage local et « commune la plus proche »
        codes = {DEPS[i]["code"] for i in self.terr_de}
        cs = [c for c in json.loads((raw / "communes_api.json").read_text())
              if c.get("codeDepartement") in codes and (c.get("mairie") or c.get("centre"))]
        self.communes = cs
        xy = np.array([(c.get("mairie") or c["centre"])["coordinates"] for c in cs], float)
        self.c_lon, self.c_lat = xy[:, 0], xy[:, 1]
        self.c_arbre = cKDTree(np.c_[self.c_lon * np.cos(np.radians(self.c_lat)), self.c_lat])
        self.c_nom = {}
        for i, c in enumerate(cs):
            self.c_nom.setdefault(norm(c["nom"]), []).append(i)
        self.etabs = json.loads((build / "sites.json").read_text()) if (build / "sites.json").exists() else []
        self.client, self.erreur_client, self.sdk = None, None, None
        self.fournisseur, self.modele = FOURNISSEUR, MODELE
        self.cle = "OPENAI_API_KEY" if FOURNISSEUR == "openai" else "ANTHROPIC_API_KEY"
        self.service = "OpenAI" if FOURNISSEUR == "openai" else "Claude"
        try:
            if FOURNISSEUR == "openai":
                import openai
                self.sdk = openai
                if os.environ.get("OPENAI_API_KEY"):
                    self.client = openai.OpenAI(max_retries=2, timeout=180)
            else:
                import anthropic
                self.sdk = anthropic
                if (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                        or (Path.home() / ".config" / "anthropic").exists()):
                    self.client = anthropic.Anthropic(max_retries=2, timeout=120)
            if self.client is None:
                self.erreur_client = f"pas de clé {self.cle}"
        except Exception as e:  # noqa: BLE001 — SDK absent
            self.erreur_client = str(e)

    @property
    def disponible(self):
        return self.client is not None

    # -------------------------------------------------------------- zones et lieux
    def zone(self, noms):
        idx = []
        for brut in noms:
            n = norm(brut)
            n = ALIAS.get(n, n)
            if n in ("france", "france entiere", "toute la france", "national", "pays", "tout le territoire"):
                idx += list(self.terr_de)
                continue
            if n in ("metropole", "france metropolitaine", "hexagone"):
                idx += [i for i in self.terr_de if self.DEPS[i]["code"] not in DROM]
                continue
            if n in ("outre mer", "outremer", "drom", "dom", "dom tom", "drom com"):
                idx += [i for i in self.terr_de if self.DEPS[i]["code"] in DROM]
                continue
            code = n.upper()
            if re.fullmatch(r"\d", code):
                code = "0" + code
            if code in self.idx_code and self.idx_code[code] in self.terr_de:
                idx.append(self.idx_code[code])
                continue
            n2 = sans_article(n)
            n2 = ALIAS.get(n2, n2)
            trouve = None
            for cand in (n, n2):
                if cand in self.n_deps:
                    trouve = [self.n_deps[cand]]
                elif cand in self.n_regs:
                    trouve = self.regions[self.n_regs[cand]]
                if trouve:
                    break
            if not trouve:
                proches = [i for k, i in self.n_deps.items() if n2 and (k.startswith(n2) or n2 in k)]
                regs = [r for k, r in self.n_regs.items() if n2 and (k.startswith(n2) or n2 in k)]
                if len(regs) == 1 and not proches:
                    trouve = self.regions[regs[0]]
                elif len(proches) == 1 and not regs:
                    trouve = proches
                else:
                    sugg = [self.DEPS[i]["nom"] for i in proches][:6] + regs[:4]
                    raise Erreur(f"zone inconnue : « {brut} »" + (f" (vouliez-vous : {', '.join(sugg)} ?)" if sugg else
                                 ". Les collectivités d'outre-mer (Saint-Martin, Polynésie…) ne sont pas couvertes."))
            idx += trouve
        idx = sorted(set(idx))
        return idx, self.libelle(idx)

    def libelle(self, idx):
        s = set(idx)
        if s == set(self.terr_de):
            return "France entière"
        for r, ids in self.regions.items():
            if s == set(ids):
                return r
        noms = [self.DEPS[i]["nom"] for i in idx]
        return ", ".join(noms[:6]) + (f" et {len(noms) - 6} autres" if len(noms) > 6 else "")

    def commune(self, nom, dep=None):
        n = norm(nom)
        n = re.sub(r"^(?:la ville de|ville de|commune de|commune d)\s+", "", n)
        ids = self.c_nom.get(n, [])
        if not ids:  # tolère « Saint Flour » / « St-Flour », articles, préfixes
            ids = [i for k, l in self.c_nom.items() if k.replace(" ", "") == n.replace(" ", "") for i in l]
        if not ids:
            ids = [i for k, l in self.c_nom.items() if len(n) >= 4 and k.startswith(n) for i in l][:30]
        if dep:
            try:
                deps = {self.DEPS[i]["code"] for i in self.zone([dep])[0]}
                ids = [i for i in ids if self.communes[i]["codeDepartement"] in deps] or ids
            except Erreur:
                pass
        if not ids:
            raise Erreur(f"commune introuvable : « {nom} »")
        ids.sort(key=lambda i: -(self.communes[i].get("population") or 0))
        return [self.communes[i] for i in ids]

    def pres_de(self, lon, lat):
        _, i = self.c_arbre.query([lon * np.cos(np.radians(lat)), lat])
        c = self.communes[int(i)]
        return f"{c['nom']} ({c['codeDepartement']})"

    @staticmethod
    def pos(c):
        return (c.get("mairie") or c["centre"])["coordinates"]

    # -------------------------------------------------------------- calculs (moteur)
    def sim(self, lon, lat, nom=None):
        return {"lon": float(lon), "lat": float(lat), "terr": self.tdp(lon, lat), "nom": nom or self.pres_de(lon, lat)}

    def minutes(self, ctx, terr, sims):
        pts = tuple((round(s["lon"], 5), round(s["lat"], 5)) for s in sims if s["terr"] == terr)
        if (terr, pts) not in ctx.minutes:
            ctx.minutes[(terr, pts)] = self.SIM[terr].minutes(list(pts)) if pts else self.T[terr].min_cel0
        return ctx.minutes[(terr, pts)]

    def stats(self, ctx, terr, sims):
        pts = tuple((round(s["lon"], 5), round(s["lat"], 5)) for s in sims if s["terr"] == terr)
        if (terr, pts) not in ctx.stats:
            ctx.stats[(terr, pts)] = statistiques(self.T[terr], self.minutes(ctx, terr, sims), self.S)
        return ctx.stats[(terr, pts)]

    def ligne_dep(self, i, v):
        d = self.DEPS[i]
        return {"code": d["code"], "nom": d["nom"], "region": d["region_nom"], "habitants": round(v["pop"]),
                "habitants_a_30_min_ou_plus": round(v["pop_loin"]), "part_a_30_min_ou_plus_pct": round(100 * v["part_loin"], 1),
                "temps_moyen_min": round(v["moy"], 1)}

    def deps_stats(self, ctx, idx, sims=None):
        sims = ctx.sims if sims is None else sims
        out = {}
        for i in idx:
            v = self.stats(ctx, self.terr_de[i], sims)["deps"].get(i)
            if v:
                out[i] = v
        return out

    def total(self, vals):
        pop = sum(v["pop"] for v in vals) or 1
        loin = sum(v["pop_loin"] for v in vals)
        return {"habitants": round(pop), "habitants_a_30_min_ou_plus": round(loin),
                "part_a_30_min_ou_plus_pct": round(100 * loin / pop, 1),
                "temps_moyen_min": round(sum(v["moy"] * v["pop"] for v in vals) / pop, 1)}

    def loin_total(self, ctx, sims, terrs):
        return sum(self.stats(ctx, c, sims)["pop_loin"] for c in terrs)

    def action_zone(self, idx):
        if set(idx) == set(self.terr_de):
            return {"type": "accueil"}
        terrs = {self.terr_de[i] for i in idx}
        regs = {self.DEPS[i]["region_nom"] for i in idx}
        if len(terrs) != 1 or len(regs) != 1:
            return None
        terr = terrs.pop()
        return {"type": "zone", "terr": terr, "region": regs.pop(),
                "dep": self.DEPS[idx[0]]["code"] if len(idx) == 1 and terr == "FR" else None}

    def temps_point(self, ctx, lon, lat):
        terr = self.tdp(lon, lat)
        if not terr:
            return None
        t = self.T[terr]
        nd, appr, _ = t.reseau.accrocher(lon, lat)
        n = int(nd[0])
        m = (float(t.T_noeud[n]) + float(appr[0])) / 60
        r = int(t.plus_proche_noeud[n])
        s = t.destinations[int(t.dest_ok[r])] if r >= 0 else None
        via = {"type": "urgences existantes", "nom": s["nom"], "ville": s.get("ville"), "finess": s["finess"]} if s else None
        for k, sm in enumerate(ctx.sims):
            if sm["terr"] != terr:
                continue
            sn, sa, _ = t.reseau.accrocher(sm["lon"], sm["lat"])
            noeuds, tn = t.elagueur.ameliores(sn[0], sa[0], LIMITE_MIN * 60)
            j = np.flatnonzero(noeuds == n)
            if len(j) and (float(tn[j[0]]) + float(appr[0])) / 60 < m:
                m = (float(tn[j[0]]) + float(appr[0])) / 60
                via = {"type": "hôpital simulé", "numero": k + 1, "pres_de": sm["nom"]}
        return {"minutes": round(m, 1) if np.isfinite(m) else None, "service_le_plus_proche": via}

    # -------------------------------------------------------------- outils
    def executer(self, nom, inp, ctx):
        if not isinstance(inp, dict):
            raise Erreur("paramètres invalides")
        f = getattr(self, "o_" + nom, None)
        if f is None:
            raise Erreur(f"outil inconnu : {nom}")
        return f(inp, ctx)

    def o_classement_departements(self, inp, ctx):
        perim = _texte(inp, "perimetre", "France") or "France"
        critere = _texte(inp, "critere", "habitants_au_dela")
        nb = _entier(inp, "nombre", 1, 30, 10)
        idx, lib = self.zone([perim])
        v = self.deps_stats(ctx, idx)
        cle = {"part_au_dela": "part_loin", "temps_moyen": "moy"}.get(critere, "pop_loin")
        tri = sorted(v, key=lambda i: -v[i][cle])
        lignes = [dict(rang=k + 1, **self.ligne_dep(i, v[i])) for k, i in enumerate(tri[:nb])]
        actions = []
        if lib == "France entière" and cle == "pop_loin":
            actions.append({"type": "classement"})
        elif (a := self.action_zone(idx)) and a["type"] == "zone":
            actions.append(a)
        res = {"perimetre": lib, "critere": critere, "seuil_min": self.S, "avec_hopitaux_simules": len(ctx.sims),
               "classement": lignes, "total_perimetre": self.total(list(v.values()))}
        return res, actions

    def o_statistiques_zone(self, inp, ctx):
        idx, lib = self.zone(_liste_textes(inp, "zones"))
        v = self.deps_stats(ctx, idx)
        res = {"zone": lib, "seuil_min": self.S, "avec_hopitaux_simules": len(ctx.sims),
               "total": self.total(list(v.values())),
               "departements": [self.ligne_dep(i, v[i]) for i in sorted(v, key=lambda i: -v[i]["pop_loin"])][:20]}
        codes = {self.DEPS[i]["code"] for i in idx}
        urg = [s for c in {self.terr_de[i] for i in idx} for s in self.T[c].destinations if s.get("dep") in codes]
        res["services_urgences"] = len(urg)
        if len(urg) <= 15:
            res["liste_urgences"] = [f"{s['nom']} ({s.get('ville') or ''})" for s in urg]
        if len(idx) == 1 and self.DEPS[idx[0]]["code"] in DROM:
            terr = self.terr_de[idx[0]]
            com = self.stats(ctx, terr, ctx.sims).get("communes", {})
            noms = {c["code"]: c["nom"] for c in self.communes}
            pires = sorted(com.items(), key=lambda kv: -kv[1]["moy"])[:6]
            res["communes_les_plus_eloignees"] = [{"commune": noms.get(k, k), "habitants": round(x["pop"]),
                                                   "temps_min": round(x["moy"], 1) if x["moy"] < 120 else "pas d'accès routier"}
                                                  for k, x in pires]
        sims_zone = [k + 1 for k, s in enumerate(ctx.sims) if s["terr"] and self.dep_du_point(s) in codes]
        if sims_zone:
            res["hopitaux_simules_dans_la_zone"] = sims_zone
        actions = []
        if _bool(inp, "afficher", True) and (a := self.action_zone(idx)):
            actions.append(a)
        return res, actions

    def dep_du_point(self, s):
        _, i = self.c_arbre.query([s["lon"] * np.cos(np.radians(s["lat"])), s["lat"]])
        return self.communes[int(i)]["codeDepartement"]

    def o_chercher_lieu(self, inp, ctx):
        cs = self.commune(_texte(inp, "nom", ""), _texte(inp, "departement"))
        c = cs[0]
        lon, lat = self.pos(c)
        tp = self.temps_point(ctx, lon, lat)
        res = {"commune": c["nom"], "code_insee": c["code"], "departement": c["codeDepartement"],
               "population": c.get("population"), "seuil_min": self.S}
        if tp is None:
            res["temps_acces"] = "point hors du réseau routier modélisé (île sans liaison, ou hors territoire couvert)"
        else:
            res["temps_route_mairie_vers_urgences_min"] = tp["minutes"]
            res["service_le_plus_proche"] = tp["service_le_plus_proche"]
        if len(cs) > 1:
            res["autres_communes_du_meme_nom"] = [f"{x['nom']} ({x['codeDepartement']})" for x in cs[1:6]]
        return res, [{"type": "centrer", "lon": lon, "lat": lat, "zoom": 10.5}]

    def o_simuler_hopitaux(self, inp, ctx):
        lieux = inp.get("lieux")
        if not isinstance(lieux, list) or not lieux or len(lieux) > 15:
            raise Erreur("« lieux » doit être une liste de 1 à 15 emplacements")
        nouveaux, ignores = [], []
        for l in lieux:
            if not isinstance(l, dict):
                raise Erreur("chaque lieu doit être un objet {commune, departement} ou {lon, lat}")
            if isinstance(l.get("lon"), (int, float)) and isinstance(l.get("lat"), (int, float)):
                s = self.sim(l["lon"], l["lat"])
            else:
                c = self.commune(_texte(l, "commune", ""), _texte(l, "departement"))[0]
                s = self.sim(*self.pos(c), nom=f"{c['nom']} ({c['codeDepartement']})")
            (nouveaux if s["terr"] else ignores).append(s)
        if not nouveaux:
            raise Erreur("aucun emplacement sur le réseau routier modélisé : " + ", ".join(s["nom"] for s in ignores))
        avant = [] if _bool(inp, "remplacer", False) else list(ctx.sims)
        apres = avant + nouveaux
        terrs = {s["terr"] for s in ctx.sims + nouveaux if s["terr"]}
        loin0 = self.loin_total(ctx, ctx.sims, terrs)
        loin1 = self.loin_total(ctx, apres, terrs)
        detail = []
        for s in nouveaux:
            seul = self.loin_total(ctx, avant, {s["terr"]}) - self.loin_total(ctx, avant + [s], {s["terr"]})
            detail.append({"pres_de": s["nom"], "habitants_ramenes_sous_30_min_a_lui_seul": round(seul)})
        idx = [i for i in self.terr_de if self.terr_de[i] in terrs]
        v0, v1 = self.deps_stats(ctx, idx, ctx.sims), self.deps_stats(ctx, idx, apres)
        touches = [dict(self.ligne_dep(i, v1[i]), habitants_a_30_min_ou_plus_avant=round(v0[i]["pop_loin"]),
                        temps_moyen_avant_min=round(v0[i]["moy"], 1))
                   for i in v1 if abs(v1[i]["pop_loin"] - v0[i]["pop_loin"]) >= 1 or abs(v1[i]["moy"] - v0[i]["moy"]) >= .05]
        touches.sort(key=lambda d: d["habitants_a_30_min_ou_plus"] - d["habitants_a_30_min_ou_plus_avant"])
        ctx.sims = apres
        res = {"hopitaux_poses": detail, "anciens_hopitaux_simules_retires": _bool(inp, "remplacer", False),
               "habitants_ramenes_sous_30_min_net": round(loin0 - loin1), "departements_touches": touches[:8],
               "nombre_total_hopitaux_simules": len(ctx.sims)}
        if ignores:
            res["ignores_hors_reseau"] = [s["nom"] for s in ignores]
        act = {"type": "placer", "remplacer": _bool(inp, "remplacer", False),
               "sites": [{"lon": s["lon"], "lat": s["lat"], "label": None} for s in nouveaux]}
        return res, [act]

    def _optim(self, inp, ctx, mode):
        idx, lib = self.zone(_liste_textes(inp, "zones"))
        terrs = {self.terr_de[i] for i in idx}
        if len(terrs) > 1:
            raise Erreur("la zone mélange plusieurs territoires (métropole et outre-mer) : faites un calcul par territoire")
        remplacer = _bool(inp, "remplacer", False)
        base = [] if remplacer else list(ctx.sims)
        p = _entier(inp, "nombre", 1, 15, 1) if mode == "mclp" else 1
        alpha = inp.get("part_population", 1.0)
        alpha = float(alpha) if isinstance(alpha, (int, float)) and not isinstance(alpha, bool) else 1.0
        try:
            r = self.optimisation([self.DEPS[i]["code"] for i in idx], mode, p, max(.5, min(1.0, alpha)),
                                  [{"lon": s["lon"], "lat": s["lat"]} for s in base])
        except ValueError as e:
            raise Erreur(str(e)) from e
        nouveaux = [dict(self.sim(s["lon"], s["lat"]), gain=s["gain"]) for s in r["sites"]]
        res = {"zone": lib, "seuil_min": self.S, "methode": "MCLP" if mode == "mclp" else "LSCP",
               "statut": r["statut"], "candidats_evalues": r.get("candidats"),
               "habitants_zone": round(r["pop_zone"]), "habitants_a_30_min_ou_plus_avant": round(r["pop_loin"]),
               "habitants_atteignables_par_un_nouveau_site": round(r["pop_couvrable"]),
               "hopitaux_simules_conserves": len(base)}
        gain = sum(s["gain"] for s in nouveaux)
        cumul = np.cumsum([s["gain"] for s in nouveaux]) if nouveaux else []
        sites = [{"rang": k + 1, "pres_de": s["nom"], "lon": round(s["lon"], 4), "lat": round(s["lat"], 4),
                  "habitants_ramenes_sous_30_min_dans_la_zone": s["gain"],
                  "part_cumulee_du_gain_pct": round(100 * float(cumul[k]) / gain, 1) if gain else 0}
                 for k, s in enumerate(nouveaux)]
        res.update(nombre_hopitaux=len(nouveaux), emplacements=sites, habitants_ramenes_sous_30_min_dans_la_zone=gain,
                   restent_a_30_min_ou_plus=round(r["pop_loin"] - gain),
                   hors_de_portee=round(max(0.0, r["pop_loin"] - r["pop_couvrable"])))
        actions = []
        if nouveaux and (mode == "mclp" or _bool(inp, "placer", True)):
            avant_loin = self.loin_total(ctx, ctx.sims, terrs)
            ctx.sims = base + [{k: s[k] for k in ("lon", "lat", "terr", "nom")} for s in nouveaux]
            v = self.deps_stats(ctx, idx)
            res["apres_dans_la_zone"] = self.total(list(v.values()))
            res["habitants_ramenes_sous_30_min_y_compris_departements_voisins"] = round(
                avant_loin - self.loin_total(ctx, ctx.sims, terrs))
            res["poses_sur_la_carte"] = True
            if (a := self.action_zone(idx)) and a["type"] == "zone":
                actions.append(a)
            actions.append({"type": "placer", "remplacer": remplacer,
                            "sites": [{"lon": s["lon"], "lat": s["lat"], "gain": s["gain"]} for s in nouveaux]})
        elif not nouveaux:
            res["message"] = ("toute la population de la zone est déjà sous le seuil" if not r["pop_loin"] else
                              "aucun site ne peut ramener ces habitants sous le seuil (zones sans route)")
        return res, actions

    def o_optimiser_implantations(self, inp, ctx):
        if "nombre" not in inp:
            raise Erreur("« nombre » est obligatoire")
        return self._optim(inp, ctx, "mclp")

    def o_combien_hopitaux(self, inp, ctx):
        return self._optim(inp, ctx, "lscp")

    def o_retirer_hopitaux(self, inp, ctx):
        nums = inp.get("numeros") or []
        if not isinstance(nums, list) or not all(isinstance(n, int) and not isinstance(n, bool) for n in nums):
            raise Erreur("« numeros » doit être une liste d'entiers")
        if not ctx.sims:
            return {"message": "aucun hôpital simulé sur la carte"}, []
        idx = sorted({n - 1 for n in nums if 1 <= n <= len(ctx.sims)}) if nums else list(range(len(ctx.sims)))
        if not idx:
            raise Erreur(f"numéros invalides : il y a {len(ctx.sims)} hôpitaux simulés")
        retires = [ctx.sims[i]["nom"] for i in idx]
        ctx.sims = [s for i, s in enumerate(ctx.sims) if i not in idx]
        return {"retires": retires, "restants": len(ctx.sims)}, [{"type": "retirer", "indices": idx}]

    def o_chercher_etablissement(self, inp, ctx):
        q = norm(_texte(inp, "recherche", ""))
        if not q:
            raise Erreur("recherche vide")
        mots = q.split()
        trouves = [s for s in self.etabs
                   if s.get("finess") == q or all(m in norm(f"{s.get('nom', '')} {s.get('nom_long', '')} {s.get('ville', '')}")
                                                  for m in mots)]
        trouves.sort(key=lambda s: (not ({"SU", "AMU"} & set(s["types"])), s.get("nom", "")))
        out = [{"nom": s.get("nom_long") or s.get("nom"), "ville": s.get("ville"), "departement": s.get("dep"),
                "finess": s.get("finess"), "statut": s.get("statut"),
                "activites": [TYPES.get(t, t) for t in s["types"]],
                "compte_comme_urgences_dans_le_calcul": bool({"SU", "AMU"} & set(s["types"]))} for s in trouves[:6]]
        res = {"resultats": out, "nombre_trouves": len(trouves)}
        acts = [{"type": "centrer", "lon": trouves[0]["lon"], "lat": trouves[0]["lat"], "zoom": 12}] if trouves and trouves[0].get("lon") else []
        return res, acts

    # -------------------------------------------------------------- cartes visuelles du chat
    def visuels(self, nom, r):
        """Petits graphiques affichés sous la réponse (l'interface les dessine)."""
        S = self.S
        if nom == "classement_departements":
            cle, f = {"part_au_dela": ("part_a_30_min_ou_plus_pct", "pct"),
                      "temps_moyen": ("temps_moyen_min", "min")}.get(r["critere"], ("habitants_a_30_min_ou_plus", "hab"))
            titre = {"pct": f"Part à {S} min ou plus", "min": "Temps moyen"}.get(f, f"Habitants à {S} min ou plus")
            return [{"k": "barres", "titre": f"{titre} · {r['perimetre']}", "f": f, "rang": True,
                     "lignes": [{"nom": x["nom"], "sous": x["code"], "v": x[cle], "min": x["temps_moyen_min"]}
                                for x in r["classement"][:6]]}]
        if nom == "statistiques_zone":
            t = r["total"]
            out = [{"k": "tuiles", "titre": f"{r['zone']} · {r['services_urgences']} service(s) d’urgences",
                    "tuiles": [{"v": t["temps_moyen_min"], "f": "min", "lib": "temps moyen", "min": t["temps_moyen_min"]},
                               {"v": t["part_a_30_min_ou_plus_pct"], "f": "pct", "lib": f"à {S} min ou plus"},
                               {"v": t["habitants_a_30_min_ou_plus"], "f": "hab", "lib": "habitants au-delà"}]}]
            if r.get("communes_les_plus_eloignees"):
                out.append({"k": "barres", "titre": "Communes les plus éloignées", "f": "min",
                            "lignes": [{"nom": c["commune"], "v": c["temps_min"] if isinstance(c["temps_min"], (int, float)) else 120,
                                        "min": c["temps_min"] if isinstance(c["temps_min"], (int, float)) else 60}
                                       for c in r["communes_les_plus_eloignees"]]})
            elif len(r["departements"]) > 1:
                out.append({"k": "barres", "titre": f"Habitants à {S} min ou plus", "f": "hab",
                            "lignes": [{"nom": d["nom"], "sous": d["code"], "v": d["habitants_a_30_min_ou_plus"],
                                        "min": d["temps_moyen_min"]} for d in r["departements"][:6]]})
            return out
        if nom == "chercher_lieu" and r.get("temps_route_mairie_vers_urgences_min") is not None:
            v = r.get("service_le_plus_proche") or {}
            vers = v.get("nom") if v.get("type") == "urgences existantes" else f"hôpital simulé n°{v.get('numero')} ({v.get('pres_de')})"
            return [{"k": "temps", "min": r["temps_route_mairie_vers_urgences_min"], "de": r["commune"], "vers": vers or "–"}]
        if nom == "simuler_hopitaux":
            out = [{"k": "chiffres", "items": [{"v": r["habitants_ramenes_sous_30_min_net"], "signe": True, "ton": "bon",
                                                "lib": f"habitants passent sous {S} min"}]}]
            baisse = [{"nom": d["nom"], "sous": d["code"], "v": d["habitants_a_30_min_ou_plus_avant"] - d["habitants_a_30_min_ou_plus"],
                       "min": d["temps_moyen_min"]} for d in r["departements_touches"]]
            baisse = [b for b in baisse if b["v"] > 0][:6]
            if baisse:
                out.append({"k": "barres", "titre": f"Habitants ramenés sous {S} min, par département", "f": "hab",
                            "signe": "+", "bon": True, "lignes": baisse})
            return out
        if nom in ("optimiser_implantations", "combien_hopitaux"):
            if not r.get("emplacements"):
                return []
            items = [{"v": r["habitants_ramenes_sous_30_min_dans_la_zone"], "signe": True, "ton": "bon",
                      "lib": f"habitants sous {S} min · {r['zone']}"}]
            if nom == "combien_hopitaux":
                items.insert(0, {"v": r["nombre_hopitaux"], "ton": "neutre", "lib": "nouveaux hôpitaux nécessaires"})
            e = r["emplacements"]
            carte = {"k": "barres", "titre": "Emplacements · gain par hôpital", "f": "hab", "signe": "+", "bon": True, "rang": True,
                     "lignes": [{"nom": x["pres_de"], "v": x["habitants_ramenes_sous_30_min_dans_la_zone"]} for x in e[:6]]}
            if len(e) > 6:
                carte["reste"] = len(e) - 6
            if r.get("hors_de_portee"):
                carte["note"] = f"{r['hors_de_portee']:,} habitants hors de portée (aucun site possible à moins de {S} min)".replace(",", " ")
            return [{"k": "chiffres", "items": items}, carte]
        if nom == "chercher_etablissement" and r.get("resultats"):
            return [{"k": "liste", "titre": "FINESS", "lignes": [
                {"nom": x["nom"], "sous": f"{x.get('ville') or ''} · {', '.join(x['activites'])}"} for x in r["resultats"][:4]]}]
        return []

    # -------------------------------------------------------------- conversation
    def etat_texte(self, ctx):
        lignes = ["[État de la carte]"]
        if ctx.sims:
            lignes.append("Hôpitaux simulés : " + " ; ".join(f"{k + 1}. près de {s['nom']}" for k, s in enumerate(ctx.sims)))
        else:
            lignes.append("Aucun hôpital simulé.")
        lignes.append("Zone affichée : " + (ctx.region or ctx.terr or "France entière"))
        return "\n".join(lignes)

    @staticmethod
    def historique(h):
        msgs = []
        for m in (h or [])[-MAX_HISTORIQUE:]:
            r, t = m.get("role"), (m.get("texte") or "").strip()
            if r not in ("user", "assistant") or not t:
                continue
            if msgs and msgs[-1]["role"] == r:
                msgs[-1]["content"] += "\n\n" + t
            else:
                msgs.append({"role": r, "content": t})
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        if msgs and msgs[-1]["role"] == "user":  # question restée sans réponse
            msgs.pop()
        return msgs

    @staticmethod
    def sans_partiel(content):
        """Après une bascule (« fallback ») en cours de réponse, on ne renvoie pas les blocs
        thinking / tool_use antérieurs à la bascule (règle de l'API)."""
        k = [i for i, b in enumerate(content) if b.type == "fallback"]
        if not k:
            return content
        return [b for i, b in enumerate(content) if i >= k[-1] or b.type not in ("thinking", "redacted_thinking", "tool_use")]

    def message_erreur(self, e):
        a = self.sdk
        if isinstance(e, a.AuthenticationError):
            return f"Clé d’API refusée : vérifiez {self.cle} dans le fichier .env, puis relancez le serveur."
        if isinstance(e, (a.PermissionDeniedError, a.NotFoundError)):
            return f"Cette clé d’API n’a pas accès au modèle {self.modele}."
        if isinstance(e, a.RateLimitError):
            return "Trop de demandes ou crédit épuisé : réessayez dans un instant."
        if isinstance(e, a.APIConnectionError):
            return f"Impossible de joindre le service {self.service} (connexion internet ?)."
        if isinstance(e, a.APIStatusError) and e.status_code >= 500:
            return f"Le service {self.service} est momentanément indisponible : réessayez."
        if isinstance(e, a.APIStatusError):
            return f"Requête refusée par l’API ({e.status_code})."
        return "Erreur inattendue de l’assistant."

    def _appel(self, id_, nom, inp, ctx):
        """Exécute un outil en émettant les événements de l'interface ; renvoie (contenu, erreur)."""
        yield {"t": "etape", "id": id_, "v": ETAPES.get(nom, nom)}
        try:
            res, actions = self.executer(nom, inp, ctx)
        except Erreur as e:
            yield {"t": "etape_ok", "id": id_, "echec": True}
            return str(e), True
        except Exception as e:  # noqa: BLE001 — une panne d'outil ne doit pas couper la conversation
            print("[assistant] outil", nom, repr(e))
            yield {"t": "etape_ok", "id": id_, "echec": True}
            return f"erreur interne du calcul : {e}", True
        ids = SOURCES_OUTIL.get(nom, [])
        res["sources"] = ids
        for a in actions:
            yield {"t": "action", "v": a}
        try:
            for c in self.visuels(nom, res):
                yield {"t": "visuel", "v": c}
        except Exception as e:  # noqa: BLE001 — un graphique raté ne bloque pas la réponse
            print("[assistant] visuel", nom, repr(e))
        if ids:
            yield {"t": "sources", "v": [dict(id=i, **SOURCES[i]) for i in ids]}
        yield {"t": "etape_ok", "id": id_}
        return json.dumps(res, ensure_ascii=False), False

    def _openai(self, systeme, messages, ctx):
        """Boucle outils avec l'API Responses d'OpenAI (état conservé par previous_response_id)."""
        outils = [{"type": "function", "name": o["name"], "description": o["description"],
                   "parameters": o["input_schema"], "strict": False} for o in OUTILS]
        entree = [{"role": m["role"], "content": m["content"] if isinstance(m["content"], str)
                   else "\n\n".join(b["text"] for b in m["content"])} for m in messages]
        prec = None
        for _ in range(MAX_APPELS):
            kw = dict(model=self.modele, instructions=systeme, tools=outils, input=entree,
                      reasoning={"effort": EFFORT}, max_output_tokens=16000)
            if prec:
                kw["previous_response_id"] = prec
            refus = ""
            try:
                with self.client.responses.stream(**kw) as flux:
                    for ev in flux:
                        if ev.type == "response.output_text.delta":
                            yield {"t": "texte", "v": ev.delta}
                        elif ev.type == "response.refusal.delta":
                            refus += ev.delta
                    rep = flux.get_final_response()
            except self.sdk.APIError as e:
                print("[assistant]", type(e).__name__, getattr(e, "message", e))
                yield {"t": "erreur", "v": self.message_erreur(e)}
                return
            if refus:
                yield {"t": "texte", "v": refus}
                return
            appels = [o for o in rep.output if o.type == "function_call"]
            if not appels:
                return
            prec, entree = rep.id, []
            for b in appels:
                try:
                    inp = json.loads(b.arguments or "{}")
                except json.JSONDecodeError:
                    inp = None  # → Erreur « paramètres invalides » renvoyée au modèle
                contenu, err = yield from self._appel(b.call_id, b.name, inp, ctx)
                entree.append({"type": "function_call_output", "call_id": b.call_id,
                               "output": ("ERREUR : " + contenu) if err else contenu})

    def _claude(self, systeme, messages, ctx):
        """Boucle outils avec l'API Messages d'Anthropic (Claude)."""
        for _ in range(MAX_APPELS):
            try:
                with self.client.beta.messages.stream(
                        model=self.modele, max_tokens=16000, system=systeme, tools=OUTILS, messages=messages,
                        output_config={"effort": EFFORT}, cache_control={"type": "ephemeral"},
                        betas=["server-side-fallback-2026-07-01"], fallbacks="default") as flux:
                    for ev in flux:
                        if ev.type == "content_block_delta" and ev.delta.type == "text_delta":
                            yield {"t": "texte", "v": ev.delta.text}
                    msg = flux.get_final_message()
            except ValueError:  # JSON d'outil illisible (streaming des paramètres)
                yield {"t": "erreur", "v": "Réponse illisible du modèle : reformulez la question."}
                return
            except TypeError as e:  # aucun identifiant résolu par le SDK
                print("[assistant]", e)
                yield {"t": "erreur", "v": f"L’assistant n’est pas configuré : ajoutez {self.cle}=… dans le "
                                           "fichier .env du projet, puis relancez le serveur."}
                return
            except self.sdk.APIError as e:
                print("[assistant]", type(e).__name__, getattr(e, "message", e))
                yield {"t": "erreur", "v": self.message_erreur(e)}
                return
            if msg.stop_reason == "refusal":
                yield {"t": "texte", "v": "\n\nJe ne peux pas répondre à cette demande. Posez-moi une question sur "
                                          "l’accès aux urgences ou l’implantation de services."}
                return
            content = self.sans_partiel(msg.content)
            appels = [b for b in content if b.type == "tool_use"]
            if msg.stop_reason == "max_tokens" or not appels or msg.stop_reason != "tool_use":
                return
            messages.append({"role": "assistant", "content": content})
            resultats = []
            for b in appels:
                contenu, err = yield from self._appel(b.id, b.name, b.input, ctx)
                r = {"type": "tool_result", "tool_use_id": b.id, "content": contenu}
                if err:
                    r["is_error"] = True
                resultats.append(r)
            messages.append({"role": "user", "content": resultats})

    def repondre(self, question, historique, etat):
        """Générateur d'événements pour l'interface : etape, etape_ok, action, visuel, sources, texte, erreur, fin."""
        if not self.client:
            yield {"t": "erreur", "v": f"L’assistant n’est pas configuré : ajoutez {self.cle}=… dans le fichier "
                                       ".env du projet, puis relancez le serveur."}
            return
        sims = []
        for s in (etat or {}).get("sites", [])[:30]:
            try:
                sims.append(self.sim(float(s["lon"]), float(s["lat"])))
            except (KeyError, TypeError, ValueError):
                continue
        ctx = Contexte(sims, (etat or {}).get("region"), (etat or {}).get("terr"))
        messages = self.historique(historique)
        messages.append({"role": "user", "content": [{"type": "text", "text": self.etat_texte(ctx)},
                                                     {"type": "text", "text": question.strip()[:2000]}]})
        boucle = self._openai if self.fournisseur == "openai" else self._claude
        try:
            yield from boucle(SYSTEME.format(seuil=self.S), messages, ctx)
        except Exception as e:  # noqa: BLE001
            print("[assistant] erreur", repr(e))
            yield {"t": "erreur", "v": "Erreur inattendue de l’assistant : réessayez."}
        yield {"t": "fin"}
