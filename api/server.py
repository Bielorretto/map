"""API « Accès urgences » (FastAPI) + service du front.

Lancement :  .venv/bin/python -m uvicorn api.server:app --port 8000
puis ouvrir http://localhost:8000
"""
import base64
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))
# Clé de l'assistant (ANTHROPIC_API_KEY) : variable d'environnement ou fichier .env du projet.
if (ROOT / ".env").exists():
    for ligne in (ROOT / ".env").read_text().splitlines():
        k, sep, v = ligne.strip().partition("=")
        if sep and k and not k.startswith("#"):
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
from api.assistant import Assistant  # noqa: E402
from api.recherche import Recherche  # noqa: E402
from config import RAW, TERRITOIRES, terr_of_dep  # noqa: E402
from engine.optimisation import Optimiseur  # noqa: E402
from engine.simulation import Simulateur, statistiques  # noqa: E402
from engine.territoire import BUILD, SEUIL_MIN, SNAP_MAX_M, Territoire  # noqa: E402

app = FastAPI(title="Accès urgences", version="1.0")
app.add_middleware(GZipMiddleware, minimum_size=2000)

DEPS = json.loads((BUILD / "departements.json").read_text())
T, SIM, OPT, BASE = {}, {}, {}, {}
for code, cfg in TERRITOIRES.items():
    if (BUILD / f"modele_{code}.npz").exists():
        t0 = time.time()
        T[code] = Territoire.charger(code, cfg)
        SIM[code] = Simulateur(T[code])
        OPT[code] = Optimiseur(T[code])
        BASE[code] = statistiques(T[code], T[code].min_cel0)
        print(f"[api] {code} chargé en {time.time() - t0:.1f}s")


def stats_json(code, st):
    out = {"pop": st["pop"], "pop_loin": st["pop_loin"], "moy": st["moy"],
           "deps": {DEPS[d]["code"]: v for d, v in st["deps"].items()}}
    if "communes" in st:
        out["communes"] = st["communes"]
    return out


def territoire_du_point(lon, lat):
    best = None
    for code, t in T.items():
        b = t.bbox
        if b[0] <= lon <= b[2] and b[1] <= lat <= b[3]:
            _, _, d = t.reseau.accrocher(lon, lat)
            if d[0] <= SNAP_MAX_M and (best is None or d[0] < best[1]):
                best = (code, d[0])
    return best[0] if best else None


def site_public(s):
    return {k: s.get(k) for k in ("finess", "nom", "nom_long", "ville", "dep", "types", "statut", "lon", "lat",
                                   "position", "source")}


def sites_en_attente():
    """Sites FINESS des territoires dont le modèle routier n'est pas encore calculé."""
    attente = [c for c in TERRITOIRES if c not in T]
    if not attente or not (BUILD / "sites.json").exists():
        return attente, [], []
    tous = json.loads((BUILD / "sites.json").read_text())
    urg, smur = [], []
    for s in tous:
        c = terr_of_dep(s["dep"])
        if c in attente:
            if {"SU", "AMU"} & set(s["types"]):
                urg.append(dict(site_public(s), terr=c))
            if {"SMUR", "SMURP", "ASMUR"} & set(s["types"]):
                smur.append(dict(site_public(s), terr=c))
    return attente, urg, smur


@app.get("/api/init")
def init():
    attente, urg_att, smur_att = sites_en_attente()
    return {
        "en_cours": attente,
        "assistant": ASSISTANT.disponible,
        "seuil": SEUIL_MIN,
        "departements": DEPS,
        "territoires": {c: {"nom": t.cfg["nom"], "bbox": list(map(float, t.bbox)), "W": t.W, "H": t.H,
                            "stats": stats_json(c, BASE[c])} for c, t in T.items()},
        "urgences": [dict(site_public(s), terr=c) for c, t in T.items() for s in t.destinations] + urg_att,
        "smur": [dict(site_public(s), terr=c) for c, t in T.items() for s in t.smur] + smur_att,
        "methode": {
            "reseau": "OpenStreetMap (Geofabrik), graphe orienté, vitesses profil voiture OSRM",
            "population": "INSEE Filosofi 2021, carreaux 200 m (métropole) ; populations légales communales (DROM)",
            "sites": "FINESS — autorisations « Médecine d'urgence » (SU, antennes, SMUR), Ministère de la Santé",
            "algorithme": "Dijkstra multi-sources (temps habitant → service le plus proche)",
        },
    }


@app.get("/api/raster/{code}.bin")
def raster(code: str):
    if code not in T:
        raise HTTPException(404)
    t = T[code]
    return Response(t.raster(t.code_pix0).tobytes(), media_type="application/octet-stream",
                    headers={"Cache-Control": "max-age=3600"})


class Point(BaseModel):
    lon: float
    lat: float


class Sites(BaseModel):
    sites: list[Point] = Field(default_factory=list)


def patch_json(code, imp):
    p = imp["patch"]
    out = {"terr": code, "lon": imp["lon"], "lat": imp["lat"]}
    if p:
        out.update(x=p["x"], y=p["y"], w=p["w"], h=p["h"], data=base64.b64encode(p["data"].tobytes()).decode())
    return out


@app.post("/api/impact")
def impact(pt: Point):
    """Pixels améliorés par un site placé en (lon, lat) — appelé pendant le glisser."""
    code = territoire_du_point(pt.lon, pt.lat)
    if not code:
        return {"terr": None}
    t0 = time.time()
    imp = SIM[code].impact(pt.lon, pt.lat)
    if imp is None:
        return {"terr": None}
    out = patch_json(code, imp)
    out["ms"] = round((time.time() - t0) * 1000)
    return out


@app.post("/api/simulation")
def simulation(req: Sites):
    """Indicateurs avec tous les sites simulés (appelé quand on relâche un hôpital)."""
    par_terr = {}
    for s in req.sites:
        c = territoire_du_point(s.lon, s.lat)
        if c:
            par_terr.setdefault(c, []).append((s.lon, s.lat))
    out, gain = {}, 0.0
    for c in T:
        if c not in par_terr:
            continue
        m = SIM[c].minutes(par_terr[c])
        st = statistiques(T[c], m)
        out[c] = stats_json(c, st)
        t = T[c]
        gain += float(t.cel_pop[(t.min_cel0 >= SEUIL_MIN) & (m < SEUIL_MIN)].sum())
    return {"territoires": out, "gain": gain}


class Optim(BaseModel):
    deps: list[str]
    mode: str = "mclp"         # "mclp" (p sites) ou "lscp" (combien de sites ?)
    p: int = 1
    alpha: float = 1.0         # LSCP : part de la population couvrable à ramener sous le seuil
    seuil: int = SEUIL_MIN
    sites: list[Point] = Field(default_factory=list)


def calcul_optimisation(dep_codes, mode="mclp", p=1, alpha=1.0, sites=(), seuil=SEUIL_MIN):
    """MCLP (p sites) ou LSCP (combien de sites ?) sur les départements donnés."""
    codes = {d["code"]: i for i, d in enumerate(DEPS)}
    idx = [codes[d] for d in dep_codes if d in codes]
    if not idx:
        raise ValueError("départements inconnus")
    terr = next((c for c, t in T.items() if np.isin(idx, t.deps).all()), None)
    if terr is None:
        raise ValueError("périmètre hors des territoires calculés")
    t0 = time.time()
    t = T[terr]
    existants = [(s["lon"], s["lat"]) for s in sites if territoire_du_point(s["lon"], s["lat"]) == terr]
    minutes = SIM[terr].minutes(existants)
    zone = np.isin(t.cel_dep, idx)
    O = OPT[terr]
    P = O.preparer(zone, minutes, seuil)
    base = {"terr": terr, "pop_zone": float(t.cel_pop[zone].sum()),
            "pop_loin": float(t.cel_pop[zone & ~(minutes < seuil)].sum())}
    if P is None:
        return dict(base, sites=[], statut="rien à améliorer", pop_couvrable=0.0, ms=0)
    if mode == "lscp":
        choix, statut = O.lscp(P, max(0.05, min(1.0, alpha)))
    else:
        choix, statut = O.mclp(P, max(1, min(15, p)))
        if choix is None:
            choix, _ = O.glouton(P, p)
            statut = "glouton"
    ordre, gains = O.ordonner(P, choix or [])
    return dict(base, sites=O.sites(P, ordre, gains), statut=statut, pop_couvrable=P["pop_couvrable"],
                candidats=int(len(P["cand"])), ms=round((time.time() - t0) * 1000))


@app.post("/api/optimisation")
def optimisation(req: Optim):
    try:
        return calcul_optimisation(req.deps, req.mode, req.p, req.alpha, [s.model_dump() for s in req.sites], req.seuil)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


ASSISTANT = Assistant(T, SIM, DEPS, territoire_du_point, calcul_optimisation, RAW, BUILD, SEUIL_MIN)
print(f"[api] assistant {ASSISTANT.fournisseur} / {ASSISTANT.modele} : "
      f"{'prêt' if ASSISTANT.disponible else 'non configuré (' + str(ASSISTANT.erreur_client) + ')'}")


RECHERCHE = Recherche(ASSISTANT)


@app.get("/api/recherche")
def recherche(q: str = ""):
    """Barre de recherche : communes, urgences/SMUR (FINESS), adresses et lieux-dits (BAN)."""
    return RECHERCHE.chercher(q)


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    historique: list[dict] = Field(default_factory=list)
    etat: dict = Field(default_factory=dict)


@app.post("/api/chat")
def chat(q: Question):
    """Assistant : flux d'événements (SSE) — étapes de calcul, actions sur la carte, texte de la réponse."""
    def flux():
        for ev in ASSISTANT.repondre(q.question, q.historique, q.etat):
            yield "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
    return StreamingResponse(flux(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


WEB = ROOT / "web"


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


app.mount("/", StaticFiles(directory=WEB), name="web")
