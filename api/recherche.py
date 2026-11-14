"""Barre de recherche : trouver n'importe quel lieu et son temps d'accès aux urgences.

Ordre des sources :
1. communes (geo.api.gouv.fr / INSEE, fichier local) : nom avec ou sans tirets/accents, « St » = « Saint » ;
2. services d'urgences et SMUR (FINESS, fichier local) : nom, nom long, ville ;
3. adresses, lieux-dits, codes postaux : Base Adresse Nationale via l'IGN Géoplateforme
   (appelée seulement si les sources locales ne suffisent pas).
Chaque résultat porte le temps de route actuel vers les urgences les plus proches (moteur, sans simulation).
"""
import json
import re
import urllib.parse
import urllib.request

import numpy as np

from .assistant import norm

BAN = "https://data.geopf.fr/geocodage/search"
NOMS_TYPES = {"SU": "Urgences", "AMU": "Antenne d’urgences", "SAMU": "SAMU", "SMUR": "SMUR",
              "SMURP": "SMUR pédiatrique", "ASMUR": "Antenne SMUR"}


class Recherche:
    def __init__(self, A):
        self.A = A
        self.cn = [norm(c["nom"]) for c in A.communes]
        self.cc = [k.replace(" ", "") for k in self.cn]
        self.cpop = np.array([c.get("population") or 0 for c in A.communes])
        sites = {}
        for code, t in A.T.items():
            for s in t.destinations:
                sites[s["finess"]] = dict(s, terr=code, service="urgences")
            for s in t.smur:
                sites.setdefault(s["finess"], dict(s, terr=code, service="SMUR"))
        self.sites = list(sites.values())
        self.sn = [norm(f'{s["nom"]} {s.get("nom_long", "")} {s.get("ville", "")}') for s in self.sites]

    # ------------------------------------------------------------------ helpers
    def dep(self, code):
        i = self.A.idx_code.get(code)
        d = self.A.DEPS[i] if i is not None else {}
        return d.get("nom", code), d.get("region_nom", "")

    def temps(self, lon, lat):
        """Temps de route actuel (min) vers les urgences les plus proches + nom du service."""
        terr = self.A.tdp(lon, lat)
        if not terr:
            return None, None, None
        t = self.A.T[terr]
        nd, appr, dist = t.reseau.accrocher(lon, lat)
        if dist[0] > 6000:
            return None, None, terr
        n = int(nd[0])
        m = (float(t.T_noeud[n]) + float(appr[0])) / 60
        r = int(t.plus_proche_noeud[n])
        s = t.destinations[int(t.dest_ok[r])] if r >= 0 else None
        return (round(m, 1) if np.isfinite(m) else None), (s["nom"] if s else None), terr

    def lieu(self, type_, nom, detail, lon, lat, zoom, **extra):
        m, via, terr = self.temps(lon, lat)
        return dict(type=type_, nom=nom, detail=detail, lon=round(float(lon), 6), lat=round(float(lat), 6),
                    zoom=zoom, minutes=m, via=via, terr=terr, **extra)

    # ------------------------------------------------------------------ sources
    def communes(self, q, n):
        qc, mots = q.replace(" ", ""), q.split()
        rangs = []
        for i, (k, c) in enumerate(zip(self.cn, self.cc)):
            if c == qc:
                r = 0
            elif c.startswith(qc):
                r = 1
            elif all(any(w.startswith(m) for w in k.split()) for m in mots):
                r = 2
            elif len(qc) >= 4 and qc in c:
                r = 3
            else:
                continue
            rangs.append((r, -self.cpop[i], i))
        rangs.sort()
        out = []
        for _, _, i in rangs[:n]:
            c = self.A.communes[i]
            dep, reg = self.dep(c["codeDepartement"])
            lon, lat = (c.get("mairie") or c["centre"])["coordinates"]
            pop = c.get("population") or 0
            out.append(self.lieu("commune", c["nom"], f'{dep} ({c["codeDepartement"]}) · {pop:,} hab.'.replace(",", " "),
                                 lon, lat, 11.2, code=c["code"], dep=c["codeDepartement"], region=reg))
        return out

    def etablissements(self, q, n):
        mots = q.split()
        ids = [i for i, k in enumerate(self.sn) if all(m in k for m in mots)]
        ids.sort(key=lambda i: (self.sites[i]["service"] != "urgences", self.sites[i]["nom"]))
        out = []
        for i in ids[:n]:
            s = self.sites[i]
            types = ", ".join(v for k, v in NOMS_TYPES.items() if k in s.get("types", []))  # urgences d’abord
            detail = " · ".join(x for x in (f'{s.get("ville", "")} ({s["dep"]})', types, f'FINESS {s["finess"]}') if x)
            out.append(self.lieu("etablissement", s["nom"], detail,
                                 s["lon"], s["lat"], 13, service=s["service"], dep=s["dep"]))
        return out

    def adresses(self, q_brut, n, deja):
        url = f"{BAN}?{urllib.parse.urlencode({'q': q_brut, 'limit': n + 3})}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "acces-urgences"}), timeout=2.5) as r:
                fs = json.load(r).get("features", [])
        except Exception:
            return []
        out = []
        for f in fs:
            p = f.get("properties", {})
            if p.get("type") == "municipality" and p.get("citycode") in deja:
                continue
            lon, lat = f["geometry"]["coordinates"]
            zoom = {"housenumber": 15.5, "street": 14.5, "locality": 13.5}.get(p.get("type"), 11.5)
            out.append(self.lieu("adresse", p.get("label", q_brut), p.get("context", ""), lon, lat, zoom))
            if len(out) >= n:
                break
        return out

    def chercher(self, q_brut, n=8):
        q_brut = (q_brut or "").strip()[:120]
        q = norm(q_brut)
        if len(q) < 2:
            return {"q": q_brut, "lieux": []}
        lieux = self.communes(q, n) + self.etablissements(q, 5)
        # BAN : adresses, lieux-dits, codes postaux, ou rien trouvé localement
        qc = q.replace(" ", "")
        territoires = sum(k.replace(" ", "").startswith(qc) for k in [*self.A.n_deps, *self.A.n_regs])
        if re.search(r"\d", q) or len(q.split()) >= 3 or (not territoires and len(lieux) < 3):
            lieux += self.adresses(q_brut, 5, {x.get("code") for x in lieux if x["type"] == "commune"})
        return {"q": q_brut, "lieux": lieux}
