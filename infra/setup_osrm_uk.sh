#!/usr/bin/env bash
# Prépare le moteur routier OSRM pour la Grande-Bretagne (profil voiture, Contraction Hierarchies).
#
#   1. télécharge great-britain-latest.osm.pbf depuis Geofabrik (reprise possible, MD5 vérifié) ;
#   2. osrm-extract  -p /opt/car.lua   : construit le graphe routier (sens uniques, interdictions,
#                                       bollards `barrier=bollard` bloquants pour les véhicules) ;
#   3. osrm-contract                   : calcule les Contraction Hierarchies (algorithme CH) ;
#
# Le résultat est lu ensuite par le conteneur `osrm-uk` (osrm-datastore + osrm-routed --shared-memory).
#
# Ressources : ~16 Go de RAM libre et ~30 Go de disque pour la préparation ; ~8-10 Go de RAM
# (mémoire partagée) pour servir la carte. Durée typique : 20-60 min selon la machine.
#
# Usage : ./setup_osrm_uk.sh [--data-dir DIR] [--image IMAGE] [--threads N] [--force] [--skip-download]
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${OSRM_DATA_DIR:-${SCRIPT_DIR}/osrm-data}"
IMAGE="${OSRM_IMAGE:-ghcr.io/project-osrm/osrm-backend:v5.27.1}"
THREADS="${OSRM_THREADS:-$(nproc 2>/dev/null || echo 4)}"
PBF_NAME="great-britain-latest.osm.pbf"
PBF_URL="${PBF_URL:-https://download.geofabrik.de/europe/${PBF_NAME}}"
BASE="${PBF_NAME%.osm.pbf}"            # great-britain-latest
MIN_DISK_GB=30
MIN_RAM_GB=16
FORCE=0
SKIP_DOWNLOAD=0

log()  { printf '\033[1;34m[osrm-setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[osrm-setup] ATTENTION:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[osrm-setup] ERREUR:\033[0m %s\n' "$*" >&2; exit 1; }
trap 'die "échec à la ligne ${LINENO} (commande: ${BASH_COMMAND})"' ERR

usage() {
    sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --data-dir)      DATA_DIR="$2"; shift 2 ;;
        --image)         IMAGE="$2"; shift 2 ;;
        --threads)       THREADS="$2"; shift 2 ;;
        --force)         FORCE=1; shift ;;
        --skip-download) SKIP_DOWNLOAD=1; shift ;;
        -h|--help)       usage 0 ;;
        *)               warn "option inconnue: $1"; usage 1 ;;
    esac
done

# --- Pré-requis -------------------------------------------------------------------------------------
command -v docker >/dev/null 2>&1 || die "docker est requis (https://docs.docker.com/engine/install/)"
docker info >/dev/null 2>&1       || die "le démon docker ne répond pas (droits ? service démarré ?)"
command -v curl >/dev/null 2>&1   || die "curl est requis"
command -v md5sum >/dev/null 2>&1 || die "md5sum est requis"

mkdir -p "${DATA_DIR}"
DATA_DIR="$(cd "${DATA_DIR}" && pwd)"
log "dossier de données : ${DATA_DIR}"
log "image OSRM         : ${IMAGE}"
log "threads            : ${THREADS}"

free_gb=$(df -Pk "${DATA_DIR}" | awk 'NR==2 {printf "%d", $4 / 1024 / 1024}')
(( free_gb >= MIN_DISK_GB )) || die "${free_gb} Go libres sur ${DATA_DIR}, ${MIN_DISK_GB} Go requis"

if [[ -r /proc/meminfo ]]; then
    ram_gb=$(awk '/MemAvailable/ {printf "%d", $2 / 1024 / 1024}' /proc/meminfo)
    (( ram_gb >= MIN_RAM_GB )) || warn "${ram_gb} Go de RAM disponibles, ${MIN_RAM_GB} Go recommandés pour osrm-extract sur la Grande-Bretagne"
fi

osrm() {  # osrm <commande> [args...] : exécute un outil OSRM dans le conteneur, données montées sur /data
    docker run --rm -t -v "${DATA_DIR}:/data" "${IMAGE}" "$@"
}

# --- 1. Téléchargement -------------------------------------------------------------------------------
PBF_PATH="${DATA_DIR}/${PBF_NAME}"
if (( SKIP_DOWNLOAD )); then
    [[ -f "${PBF_PATH}" ]] || die "--skip-download demandé mais ${PBF_PATH} est absent"
    log "téléchargement ignoré"
else
    log "téléchargement de ${PBF_URL} (reprise automatique si interrompu)"
    curl --fail --location --retry 5 --retry-delay 5 --continue-at - --output "${PBF_PATH}" "${PBF_URL}"
    log "vérification MD5"
    expected="$(curl --fail --silent --location --retry 3 "${PBF_URL}.md5" | awk '{print $1}')"
    [[ -n "${expected}" ]] || die "MD5 de référence introuvable (${PBF_URL}.md5)"
    actual="$(md5sum "${PBF_PATH}" | awk '{print $1}')"
    [[ "${expected}" == "${actual}" ]] || die "MD5 invalide pour ${PBF_NAME} (attendu ${expected}, obtenu ${actual}) : supprimez le fichier et relancez"
    log "MD5 OK (${actual})"
fi

# --- 2. Extraction du graphe (profil voiture) -----------------------------------------------------------
if (( FORCE )) || [[ ! -f "${DATA_DIR}/${BASE}.osrm.ebg" ]]; then
    log "osrm-extract (profil voiture /opt/car.lua) — long et gourmand en RAM"
    osrm osrm-extract -p /opt/car.lua -t "${THREADS}" "/data/${PBF_NAME}"
else
    log "extraction déjà faite (--force pour refaire)"
fi

# --- 3. Contraction Hierarchies -----------------------------------------------------------------------
if (( FORCE )) || [[ ! -f "${DATA_DIR}/${BASE}.osrm.hsgr" ]]; then
    log "osrm-contract (Contraction Hierarchies)"
    osrm osrm-contract -t "${THREADS}" "/data/${BASE}.osrm"
else
    log "contraction déjà faite (--force pour refaire)"
fi

[[ -f "${DATA_DIR}/${BASE}.osrm.hsgr" ]] || die "${BASE}.osrm.hsgr absent après contraction"

log "terminé. Fichiers prêts dans ${DATA_DIR}"
cat <<EOF

Étapes suivantes :
  cd ${SCRIPT_DIR}
  cp .env.example .env            # puis renseigner les clés API
  OSRM_DATA_DIR='${DATA_DIR}' docker compose up -d --build
  curl 'http://127.0.0.1:5000/table/v1/driving/-0.1278,51.5074;-0.1419,51.5014?annotations=duration'
EOF
