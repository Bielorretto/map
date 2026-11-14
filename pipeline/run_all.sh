#!/bin/sh
# Pipeline complet : téléchargement des données publiques puis calcul des modèles.
set -e
cd "$(dirname "$0")/.."
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
command -v osmium >/dev/null || { echo "osmium-tool requis (brew install osmium-tool)"; exit 1; }
RAW=data/raw; mkdir -p $RAW
dl() { [ -s "$RAW/$1" ] || { echo "↓ $1"; curl -sL -o "$RAW/$1" "$2"; }; }
DG=https://static.data.gouv.fr/resources
dl finess_autorisations.csv $DG/finess-extraction-des-autorisations-dactivites-de-soin/20260512-091541/etalab-cs1100503-stock-20260512-0339.csv
dl amm.xml.gz $DG/finess-extraction-des-autorisations-dactivites-de-soin/20260512-091929/amm-arhgos-20260505-0140.xml.gz
[ -s $RAW/amm.xml ] || gunzip -k $RAW/amm.xml.gz
dl as_historique.zip $DG/finess-extraction-des-autorisations-dactivites-de-soins/20260108-153028/etalab-stock-as-historique-2004-2025.zip
[ -d $RAW/as_hist ] || unzip -q -o $RAW/as_historique.zip -d $RAW/as_hist
dl finess_etab_geo.csv $DG/finess-extraction-du-fichier-des-etablissements/20260512-091152/etalab-cs1100507-stock-20260512-0339.csv
dl filosofi_200m_2021.parquet $DG/revenus-pauvrete-et-niveau-de-vie-donnees-carroyees-dispositif-fichier-localise-social-et-fiscal-filosofi/20260309-120901/carreaux-200m-met-3035-2021.parquet
dl departements.geojson https://etalab-datasets.geo.data.gouv.fr/contours-administratifs/2024/geojson/departements-100m.geojson
dl regions.geojson https://etalab-datasets.geo.data.gouv.fr/contours-administratifs/2024/geojson/regions-100m.geojson
dl communes_api.json "https://geo.api.gouv.fr/communes?fields=nom,code,population,centre,mairie,codeDepartement,codeRegion&format=json"
dl france-latest.osm.pbf https://download.geofabrik.de/europe/france-latest.osm.pbf
for r in guadeloupe martinique guyane reunion mayotte; do dl $r-latest.osm.pbf https://download.geofabrik.de/europe/france/$r-latest.osm.pbf; done

mkdir -p data/build
: > data/build/hopitaux-osm.geojsonseq
for f in france guadeloupe martinique guyane reunion mayotte; do
  osmium tags-filter -O -o data/build/h.pbf $RAW/$f-latest.osm.pbf nwr/amenity=hospital nwr/healthcare=hospital
  osmium export -O -f geojsonseq -o data/build/h.geojsonseq data/build/h.pbf
  cat data/build/h.geojsonseq >> data/build/hopitaux-osm.geojsonseq; rm data/build/h.pbf data/build/h.geojsonseq
done
cd pipeline
../.venv/bin/python sites.py
../.venv/bin/python population.py
for t in FR GP MQ GF RE YT; do ../.venv/bin/python roads.py $t; done
../.venv/bin/python access.py FR GP MQ GF RE YT
echo "✓ modèles prêts — lancez ./lancer.sh"
