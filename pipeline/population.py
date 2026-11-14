"""Étape 3 — Demande : où habitent les gens ?

* Métropole : carreaux INSEE Filosofi 2021 de 200 m (population « ind »),
  rattachés à leur département (contours Etalab / IGN Admin Express 2024).
* DROM : population légale par commune (geo.api.gouv.fr / INSEE), placée à la
  mairie (le carroyage Filosofi publié ne couvre pas les DROM).
"""
import json

import numpy as np
import pyarrow.parquet as pq
import shapely
from pyproj import Transformer

from config import BUILD, RAW, TERRITOIRES


def departements():
    deps = json.load(open(RAW / "departements.geojson"))["features"]
    regs = {f["properties"]["code"]: f["properties"]["nom"]
            for f in json.load(open(RAW / "regions.geojson"))["features"]}
    meta = [{"code": f["properties"]["code"], "nom": f["properties"]["nom"],
             "region": f["properties"]["region"], "region_nom": regs.get(f["properties"]["region"], "")}
            for f in deps]
    geoms = [shapely.geometry.shape(f["geometry"]) for f in deps]
    return meta, geoms


def metropole(meta, geoms):
    t = pq.read_table(RAW / "filosofi_200m_2021.parquet", columns=["idcar_200m", "ind"]).to_pandas()
    ids = t["idcar_200m"].str.extract(r"N(\d+)E(\d+)").astype(np.int64)
    y = ids[0].to_numpy() + 100.0  # centre du carreau (coin sud-ouest + 100 m)
    x = ids[1].to_numpy() + 100.0
    pop = t["ind"].to_numpy(np.float32)
    lon, lat = Transformer.from_crs(3035, 4326, always_xy=True).transform(x, y)
    metro = [i for i, m in enumerate(meta) if not m["code"].startswith(("97", "98"))]
    tree = shapely.STRtree([geoms[i] for i in metro])
    pts = shapely.points(lon, lat)
    src, dst = tree.query(pts, predicate="intersects")
    dep = np.full(len(pts), -1, np.int32)
    dep[src] = np.asarray(metro)[dst]
    manque = np.flatnonzero(dep < 0)  # littoral : carreau hors du contour simplifié
    if len(manque):
        near = tree.query_nearest(pts[manque], return_distance=False)
        dep[manque[near[0]]] = np.asarray(metro)[near[1]]
    print(f"  FR : {len(pop):,} carreaux, {pop.sum():,.0f} habitants")
    np.savez_compressed(BUILD / "pop_FR.npz", lon=lon, lat=lat, pop=pop, dep=dep)


def outre_mer(meta):
    communes = json.load(open(RAW / "communes_api.json"))
    idx = {m["code"]: i for i, m in enumerate(meta)}
    for terr, cfg in TERRITOIRES.items():
        if "dep" not in cfg:
            continue
        cs = [c for c in communes if c["codeDepartement"] == cfg["dep"] and c.get("population")]
        lon = np.array([(c.get("mairie") or c["centre"])["coordinates"][0] for c in cs])
        lat = np.array([(c.get("mairie") or c["centre"])["coordinates"][1] for c in cs])
        pop = np.array([c["population"] for c in cs], np.float32)
        dep = np.full(len(cs), idx[cfg["dep"]], np.int32)
        np.savez_compressed(BUILD / f"pop_{terr}.npz", lon=lon, lat=lat, pop=pop, dep=dep,
                            commune=np.array([c["code"] for c in cs]), nom=np.array([c["nom"] for c in cs]))
        print(f"  {terr} : {len(cs)} communes, {pop.sum():,.0f} habitants")


def main():
    BUILD.mkdir(parents=True, exist_ok=True)
    meta, geoms = departements()
    (BUILD / "departements.json").write_text(json.dumps(meta, ensure_ascii=False))
    outre_mer(meta)
    metropole(meta, geoms)


if __name__ == "__main__":
    main()
