# Accès Urgences — temps d'accès réels aux services d'urgences

Outil d'aide à la décision : carte des temps de route jusqu'au service d'urgences
le plus proche (métropole + DROM), simulation d'un nouveau site en direct et
optimisation des implantations.

## Démarrer

```bash
./pipeline/run_all.sh     # une fois : télécharge les données et calcule les modèles (~30 min)
./lancer.sh               # lance l'API + l'interface → http://localhost:8000
```

## Données (toutes publiques, réelles)

| Besoin | Source | Fichier |
|---|---|---|
| Services d'urgences (SU, antennes), SMUR, SAMU | FINESS — autorisations d'activités de soins « Médecine d'urgence », ancien format + nouveau format AMM (réforme 2023), Ministère de la Santé, data.gouv.fr | `finess_autorisations.csv`, `amm.xml` |
| Coordonnées des établissements | FINESS établissements géolocalisés (Lambert 93 / UTM DROM) | `finess_etab_geo.csv` |
| Population (métropole) | INSEE Filosofi 2021, carreaux de 200 m (2,3 M carreaux) | `filosofi_200m_2021.parquet` |
| Population (DROM) | Populations légales communales, placées à la mairie (geo.api.gouv.fr) | `communes_api.json` |
| Réseau routier | OpenStreetMap, extraits Geofabrik (France + 5 DROM) | `*.osm.pbf` |
| Limites administratives | Etalab / IGN Admin Express 2024 | `departements.geojson`, `regions.geojson` |

Destinations retenues : structures des urgences générales (SU) et antennes de
médecine d'urgence. Les urgences uniquement pédiatriques (SUP) sont exclues
du calcul « tout public » mais restent dans les données.

## Méthode

1. **Graphe routier** (`pipeline/roads.py`) : voies carrossables OSM (autoroutes →
   rues résidentielles), découpées aux intersections et tous les 800 m, sens
   uniques et giratoires respectés. Vitesse = `maxspeed × 0,8` sinon profil par
   type de voie (valeurs du profil voiture d'OSRM).
2. **Temps de base** (`engine/territoire.py`) : un Dijkstra multi-sources sur le
   graphe inversé depuis tous les services → temps « habitant → urgences la plus
   proche » pour chaque nœud. Chaque carreau de population et chaque pixel de la
   carte est accroché au nœud le plus proche (+ trajet d'approche).
3. **Simulation** (`engine/simulation.py`, `engine/dijkstra.py`) : Dijkstra
   *élagué* depuis le nouveau site, qui s'arrête dès qu'un nœud n'est plus
   amélioré. Le résultat est exact et prend 1 à 20 ms, d'où la mise à jour en
   direct pendant le glisser.
4. **Optimisation** (`engine/optimisation.py`) :
   - *MCLP* (Church & ReVelle 1974) : où placer *p* sites pour ramener le plus
     d'habitants sous 30 min ;
   - *LSCP* (Toregas 1971) : nombre minimal de sites pour que toute la
     population atteignable passe sous 30 min.
   Les candidats sont les centres de gravité de la population sur des mailles de
   4 km, et la couverture se calcule par Dijkstra élagué. La résolution est
   exacte en programmation linéaire en nombres entiers (HiGHS via
   `scipy.optimize.milp`), avec un algorithme glouton en secours.
5. **Validation** (`pipeline/validation.py`) : comparaison trajet par trajet avec
   OSRM et l'API d'itinéraire IGN.

### Résultats de validation

| Territoire | vs OSRM (écart absolu moyen) | corrélation | vs IGN bdtopo-osrm |
|---|---|---|---|
| Métropole (60 trajets tirés au hasard, pondérés par la population) | 0,7 min (médiane −0,3) | 0,992 | IGN ≈ ×1,6 plus lent (corr. 0,945) |
| La Réunion (15 trajets) | 0,6 min | 0,993 | IGN ≈ ×1,5 plus lent |

Indicateur national obtenu : 6,1 % de la population métropolitaine à 30 min ou plus
d'un service d'urgences, cohérent avec l'ordre de grandeur publié par la DREES (~6 %).

Les temps sont « à vide » (sans trafic), comme OSRM. L'IGN applique des vitesses
plus prudentes. Un coefficient de calibration peut être ajouté si on vise le
distancier METRIC de l'INSEE.

## API

| Route | Rôle |
|---|---|
| `GET /api/init` | sites, statistiques par département / commune, métadonnées |
| `GET /api/raster/{T}.bin` | raster des temps (1 octet = minutes × 2 ; 254 hors terre ; 255 sans route) |
| `POST /api/impact` `{lon,lat}` | zone améliorée par un nouveau site (patch raster) |
| `POST /api/simulation` `{sites:[…]}` | indicateurs avec tous les sites simulés |
| `POST /api/optimisation` `{deps, mode:"mclp"|"lscp", p}` | meilleures implantations |
| `POST /api/chat` `{question, historique, etat}` | assistant : flux SSE (étapes, actions sur la carte, graphiques, sources, texte) |
| `GET /api/recherche?q=` | barre de recherche : communes (INSEE), urgences/SMUR (FINESS), adresses et lieux-dits (BAN, IGN Géoplateforme), avec le temps d’accès actuel |

## Assistant (le cœur)

Un clic sur le cœur ouvre le chat. L'assistant (OpenAI `gpt-6-astra` par défaut, ou Claude
`claude-opus-5-5`) ne calcule
rien lui-même : il appelle les outils du moteur ci-dessous, et chaque chiffre de sa
réponse vient de leur résultat. Il répond en une ou deux phrases ; le détail
s'affiche en petits graphiques sous la réponse. Chaque affirmation porte l'étiquette
de sa source ([FINESS], [INSEE], [OSM], [OPTIM], [COMMUNES]) et la liste des sources
s'affiche en bas de la réponse avec leurs liens.

| Outil | Question type | Sur la carte |
|---|---|---|
| `classement_departements` | « Quels départements sont prioritaires ? » | ouvre le classement |
| `statistiques_zone` | « Temps moyen dans le Cantal ? » | isole la zone |
| `chercher_lieu` | « Hôpital le plus proche de Corte ? » | centre sur la commune |
| `simuler_hopitaux` | « Et si on ouvrait des urgences à Langogne ? » | pose l'hôpital |
| `optimiser_implantations` | « Où placer 3 hôpitaux en Bretagne ? » (MCLP) | pose les hôpitaux |
| `combien_hopitaux` | « Combien d'hôpitaux pour mettre la Lozère au vert ? » (LSCP) | pose les hôpitaux |
| `retirer_hopitaux` | « Retire le 2 » | retire |
| `chercher_etablissement` | « Où est l'hôpital Purpan ? » (FINESS) | centre sur l'établissement |

Configuration : `cp .env.example .env`, renseigner `OPENAI_API_KEY` (ou `ANTHROPIC_API_KEY`),
relancer le serveur. La clé reste côté serveur, dans `.env` (ignoré par git). Modèle et effort
de raisonnement se règlent avec `ASSISTANT_MODELE` et `ASSISTANT_EFFORT`.
