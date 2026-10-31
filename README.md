# UK Last-Mile Hybrid Router

Optimiseur de tournées de livraison pour le Royaume-Uni.
Il scanne l'étiquette sur le téléphone, géocode la porte exacte, puis calcule la tournée.
Le livreur peut garer le van une fois et livrer plusieurs portes à pied quand c'est plus court.

**Premier test sur de vrais téléphones : voir [GUIDE_TEST.md](GUIDE_TEST.md).**

## Arborescence

```
.
├── backend/                  FastAPI + uvloop, OR-Tools, NumPy, Redis, Gemini
│   ├── config.py             tous les paramètres (marche 1,65 m/s, seuil 160 m, URLs, clés)
│   ├── uk_address_parser.py  regex postcode UK, Flat / House Name / numéro, bruit d'étiquette, clé de cache
│   ├── geocoder_uk.py        cache Redis + Ideal Postcodes / OS Places / Google ROOFTOP + repli synthétique
│   ├── vision_extractor.py   Gemini 2.5 Flash, schéma Pydantic strict (étiquettes abîmées)
│   ├── hybrid_matrix.py      Haversine NumPy, client OSRM Table API, fusion WALK_SHORTCUT
│   ├── routing_engine.py     OR-Tools, ParkingClusters, comparaison avec le van strict
│   ├── main.py               WS /ws/scan, POST /api/v1/extract-image, POST /api/v1/optimize-route
│   └── tests/                134 tests (dont un vrai redis-server)
├── infra/
│   ├── docker-compose.yml    backend + redis + osrm-uk (mémoire partagée) : pile complète
│   ├── setup_osrm_uk.sh      télécharge great-britain-latest.osm.pbf, osrm-extract, osrm-contract (CH)
│   ├── osrm/start_osrm.sh    osrm-datastore puis osrm-routed --shared-memory
│   └── .env.example
├── render.yaml               serveur en un clic sur Render (backend seul, OSRM public de démo)
├── .github/workflows/android-apk.yml   construit l'APK et le publie (release « android-latest »)
├── mobile/                   React Native (TypeScript) + projets android/ et ios/
│   ├── App.tsx
│   ├── src/UKFastScanner.tsx       caméra, OCR embarqué limité au cadre jaune, WebSocket, repli WebP
│   ├── src/DriverRouteScreen.tsx   « Garez ici → Livrez N portes à pied → Reprenez le van »
│   ├── src/SettingsScreen.tsx      adresse du serveur + clé API (stockées sur le téléphone)
│   └── src/…                       parseur TS, client WS, stores, encodeur WebP (Skia)
├── shared/address_fixtures.json    37 étiquettes : valident le parseur Python ET TypeScript
└── benchmark_uk_speed.py           50 colis, pièges UK, comparaison Van strict / Hybride
```

## Démarrer

```bash
# Tests et benchmark (aucun service externe requis)
pip install -r backend/requirements-dev.txt
(cd backend && pytest)
python benchmark_uk_speed.py                    # OSRM simulé en mémoire
python benchmark_uk_speed.py --scenario control # 50 colis sans piège : gain attendu = 0
python benchmark_uk_speed.py --osrm-url http://localhost:5000 --redis-url redis://localhost:6379/0

# Pile complète (Docker)
infra/setup_osrm_uk.sh                          # une fois : ~30 Go de disque, ~16 Go de RAM, 20 à 60 min
cd infra && cp .env.example .env && docker compose up -d --build

# Mobile
cd mobile && npm install && npm run typecheck && npm test
```

## Comment ça marche

1. **Scan (fast-path, ~95 % des colis).** L'OCR tourne sur le téléphone. Le parseur local extrait
   code postal + numéro ou nom de maison. Le téléphone envoie seulement
   `{"i":17,"p":"LS61AB","b":"14A"}` (31 octets) sur un WebSocket resté ouvert. Aucune image.
2. **Géocodage.** Clé `POSTCODE:BÂTIMENT` lue dans Redis (un seul `MGET` par lot).
   En cas de miss : Ideal Postcodes, puis OS Places, puis Google ROOFTOP.
   Une réponse PAF contient tout le code postal : on met tout en cache. Un seul appel par code postal.
3. **Scan (slow-path, étiquette déchirée).** Le cadre de visée est découpé dans un instantané,
   compressé en WebP q70 en mémoire (< 15 Ko visés), puis lu par Gemini avec un schéma strict.
4. **Matrices.** `M_van` vient d'OSRM (CH). `T_walk = D_haversine/1,65 + 10`, tout en NumPy.
   `M_hybrid` prend `T_walk` si `D ≤ 160 m` et `T_walk < M_van`. Le dépôt n'est jamais piéton.
5. **Solveur.** OR-Tools, `PATH_CHEAPEST_ARC` + `GUIDED_LOCAL_SEARCH`, 150 ms.
   Il tourne dans un pool de processus : OR-Tools garde le GIL et bloquerait sinon la boucle d'événements.
6. **ParkingClusters.** Trois plans sont chiffrés avec le **retour à pied jusqu'au van**.
   Le plus court est retenu. Il ne peut donc jamais être plus long que le van strict.

### Protocole WebSocket `/ws/scan`

| Sens | Message |
|---|---|
| client → serveur | `{"i":17,"p":"LS61AB","b":"14A"}` (+ `f` appartement, `s` rue si ça tient en 64 octets) |
| client → serveur | trame binaire : 4 octets d'id (big-endian) + image WebP |
| serveur → client | `{"i":17,"ok":1,"la":53.8,"lo":-1.55,"src":"cache","pr":"uprn","k":"LS61AB:14A","ms":0.3}` |
| serveur → client | `{"i":17,"ok":0,"e":"bad_address"\|"geocode_failed"\|…}` |

## Résultats mesurés

Mesures sur 4 cœurs, Python 3.13. Reproductibles avec `benchmark_uk_speed.py`.

| Étape (50 colis) | Médiane | p95 | Objectif |
|---|---:|---:|---|
| Matrice NumPy (51×51) | 0,17 ms | 0,23 ms | < 1 ms ✓ |
| Matrice NumPy seule 101×101 / 201×201 | 0,25 / 0,92 ms | 0,29 / 1,04 ms | < 1 ms ✓ |
| Géocodage, cache chaud (50 adresses) | 0,21 ms mémoire · 0,76 ms Redis réel | | < 0,2 ms/adresse ✓ |
| Solveur OR-Tools | 150,6 ms | 151,5 ms | 150 ms ✓ |
| Total adresses → plan | 160,7 ms | 163,4 ms | |
| WebSocket, aller-retour en boucle locale (uvicorn réel + MGET Redis réel) | 0,4 ms | 1,9 ms (p99) | < 10 ms ✓ |

| Scénario | Van strict | Plan retenu | Gain |
|---|---:|---:|---:|
| 50 colis avec pièges (LTN, impasses, sens unique, House Names) | 46 min 28 s | 45 min 43 s | **45 s** (1,6 % de la tournée, 3,5 % de la conduite) |
| 50 colis sans piège (témoin) | 36 min 42 s | 36 min 42 s | 0 s |

Le gain est modeste et c'est normal : sur 50 colis, seules 2 « poches » valent la marche.
Voir ci-dessous.

## Ce que le benchmark a révélé

Le cahier des charges demande de résoudre OR-Tools sur `M_hybrid`, puis de grouper les arcs
`WALK_SHORTCUT`. Ce modèle a un défaut : un arc de marche `i → j` **téléporte le van** de `i` à `j`.
Le solveur en profite : il traverse un filtre LTN à pied, puis repart en van de l'autre côté.
Ce gain n'existe pas. Une fois le retour à pied jusqu'au van compté, ce plan est **plus long**
que le van strict (47 min 09 s contre 46 min 28 s sur le scénario avec pièges).

Ce qui a été fait :

- La stratégie `HYBRID_SPEC` suit le cahier des charges à la lettre, et son coût est recalculé honnêtement.
- J'ai ajouté `HYBRID_POCKETS` : sur la tournée du van strict, une poche de colis isolés derrière
  une barrière est rattachée au stationnement d'un voisin. Le van reste à un seul endroit.
  C'est cette stratégie qui gagne ici.
- Le plan retenu est le plus court des trois. Le scénario témoin le prouve : 0 s de gain, aucun gain inventé.
- Mesure faite à la main : marcher 5 portes (200 à 260 s) ne bat presque jamais une excursion en van
  de 100 à 200 s. Le gain vient de 1 à 3 colis isolés derrière une barrière, avec une excursion de plusieurs minutes.

## Écarts avec le cahier des charges

- **OCR iOS** : le module utilisé (`@react-native-ml-kit/text-recognition`) emploie Google ML Kit
  sur Android **et** iOS. Ce n'est pas Apple VisionKit. Le code Swift/Kotlin natif n'est pas écrit ici.
- **Pas de Frame Processor** : l'app lit un instantané de l'aperçu toutes les ~0,6 s au lieu d'analyser ~8 images/s.
  Le Frame Processor demande `react-native-worklets-core`, qui plantait en natif (SIGSEGV, mutex détruit) sur un
  Honor sous Android 13. L'instantané n'utilise que des API stables de VisionCamera et de ML Kit.
- **`ORJSONResponse`** : la classe de FastAPI est dépréciée (v0.162). Une classe équivalente, basée sur `orjson`, est dans `main.py`.
- **ROI du chemin lent** : c'est le cadre de visée à l'écran, pas la boîte englobante de l'OCR
  (l'OCR a justement échoué dans ce cas).
- **Solveur** : processus et non threads, à cause du GIL (mesuré : deux résolutions en threads = 305 ms).
- **Gemini** : la cible « < 200 ms » du chemin lent ne dépend pas de ce code. Elle dépend de la latence de l'API Gemini.

## Ce qui n'a PAS été vérifié

- **L'app sur un appareil.** Le TypeScript passe `tsc --noEmit` en mode strict et compile avec Babel ;
  87 tests Node couvrent la logique (parseur, client WS, anti-doublon, progression,
  réglages, filtre du cadre). L'APK Android est construit par GitHub Actions. Mais la caméra, l'OCR, Skia et
  `takeSnapshot` n'ont jamais tourné sur un téléphone. L'orientation de l'instantané iOS est à valider,
  et le filtre « texte dans le cadre » suppose que ML Kit renvoie des coordonnées d'image redressée.
  Le projet `ios/` est généré mais jamais compilé.
- **Docker et le vrai OSRM.** Le démon Docker n'était pas disponible. `docker-compose.yml` est valide en YAML
  et les scripts passent `bash -n`, mais aucun conteneur n'a démarré. Les temps OSRM du benchmark viennent
  d'un OSRM **simulé** en mémoire : ils ne disent rien sur OSRM + CH sur la carte de Grande-Bretagne.
- **Les API Ideal Postcodes, OS Places, Google, Gemini.** Testées avec des réponses simulées, aux formats
  de leur documentation. Aucune vraie clé n'a été utilisée.
- **Le scénario** est fictif. Les adresses et les rues sont inventées. La marche est à vol d'oiseau :
  elle suppose un passage piéton entre les portes (ni rivière, ni voie ferrée, ni clôture).

## Hébergement

- **Serveur** : Render ou Railway (conteneur, WebSocket, processus longs). Voir `render.yaml` et `backend/railway.json`.
  Mesuré : environ 240 Mo de mémoire.
- **Vercel ne convient pas au serveur** : pas de WebSocket, pas de processus longs, et OR-Tools + NumPy pèsent
  plus de 250 Mo installés. Vercel pourrait héberger une page de démonstration statique (travail commencé : `scripts/export_demo.py`).
- **OSRM** : le serveur de démonstration public suffit pour quelques testeurs. Pour du volume, utilisez la pile
  Docker (`infra/`) sur une machine de 16 Go de RAM.

## Configuration

Tout est dans `backend/config.py` (variables d'environnement). Les principales :

| Variable | Défaut | Rôle |
|---|---|---|
| `WALK_SPEED_MPS` / `WALK_FIXED_S` / `WALK_MAX_DISTANCE_M` | 1,65 / 10 / 160 | modèle piéton |
| `SERVICE_TIME_S` | 30 | remise d'un colis (même valeur pour le van strict et l'hybride) |
| `SOLVER_TIME_LIMIT_MS` / `SOLVER_WORKERS` | 150 / 2 | solveur (≥ 2 processus : hybride et van en parallèle) |
| `APP_ENV` | development | `production` coupe le géocodage synthétique de repli et la doc OpenAPI |
| `API_KEY` | vide | si défini : `X-API-Key` (HTTP) ou `?key=` (WebSocket) |
| `POSTCODES_IO_ENABLED` | true | repli gratuit : centre du code postal (précision `postcode`), sans clé |
| `OSRM_MAX_TABLE_SIZE` | 250 | doit rester ≤ `--max-table-size` du serveur OSRM (500 dans le compose) |

Le point synthétique de repli est **fictif**. Il sert aux tests et à la démo. Il est toujours étiqueté
`synthetic` dans les réponses, et jamais mis en cache.
