#!/bin/sh
# Point d'entrée du conteneur osrm-uk : charge la carte CH dans la mémoire partagée puis sert l'API.
set -eu

BASE="/data/${OSRM_BASENAME:-great-britain-latest.osrm}"

if [ ! -f "${BASE}.hsgr" ]; then
    echo "ERREUR: ${BASE}.hsgr introuvable. Préparez la carte avec: infra/setup_osrm_uk.sh" >&2
    exit 1
fi

echo "osrm-datastore: chargement de ${BASE} en mémoire partagée..."
osrm-datastore "${BASE}"

echo "osrm-routed: algorithme CH, --shared-memory, table max ${OSRM_MAX_TABLE_SIZE:-500} points"
exec osrm-routed \
    --algorithm ch \
    --shared-memory \
    --ip 0.0.0.0 \
    --port 5000 \
    --threads "${OSRM_THREADS:-4}" \
    --max-table-size "${OSRM_MAX_TABLE_SIZE:-500}"
