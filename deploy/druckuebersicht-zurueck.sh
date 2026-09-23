#!/usr/bin/env bash
# Fork: Druckuebersicht
#
# Zurueck zum offiziellen Bambuddy-Image: stellt die docker-compose.yml von
# vor der Umstellung wieder her und startet neu. Die Daten bleiben; die
# zusaetzlichen Tabellen des Lager-Autodrucks stoeren das Original nicht.
#
# Aufruf im Ordner mit der docker-compose.yml:
#   bash druckuebersicht-zurueck.sh

set -euo pipefail

COMPOSE_DATEI="docker-compose.yml"

[[ -f "$COMPOSE_DATEI.vorher" ]] || {
    echo "FEHLER: $COMPOSE_DATEI.vorher nicht gefunden - bitte im Ordner der Installation ausfuehren." >&2
    exit 1
}
cp "$COMPOSE_DATEI" "$COMPOSE_DATEI.druckuebersicht"
cp "$COMPOSE_DATEI.vorher" "$COMPOSE_DATEI"
docker compose pull bambuddy
docker compose up -d bambuddy
echo "Wieder beim Original. Die Druckuebersicht-Einstellungen liegen in $COMPOSE_DATEI.druckuebersicht."
shopt -s nullglob
sicherungen=(bambuddy-daten-*.tgz)
echo "Datensicherungen von der Umstellung: ${sicherungen[*]:-keine}"
