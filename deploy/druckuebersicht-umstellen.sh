#!/usr/bin/env bash
# Fork: Druckuebersicht
#
# Stellt eine bestehende Bambuddy-Installation (Docker Compose mit dem
# offiziellen Image) auf die Druckuebersicht um - mit denselben Daten,
# Ports und Einstellungen.
#
# Aufruf im Ordner mit der bisherigen docker-compose.yml:
#   bash druckuebersicht-umstellen.sh
#
# Ablauf:
#   1. Pruefungen (Docker, docker-compose.yml, Dienst "bambuddy", Speicherplatz)
#   2. Quellcode holen/aktualisieren und Image bauen (Bambuddy laeuft solange weiter)
#   3. Bambuddy stoppen, Daten sichern, docker-compose.yml sichern und anpassen
#   4. Starten und pruefen, ob die Druckuebersicht antwortet
# Zurueck zum Original: bash druckuebersicht-zurueck.sh
#
# Einstellbar ueber Umgebungsvariablen:
#   QUELLE  Ordner fuer den Quellcode        (Standard: ~/druckuebersicht-src)
#   ZWEIG   Git-Branch                        (Standard: main)
#   PORT    Port von Bambuddy                 (Standard: 8000)
#   JA=1    ohne Rueckfrage

set -euo pipefail

REPO="https://github.com/jktzhitrl/bambuddy.git"
IMAGE="druckuebersicht:latest"
QUELLE="${QUELLE:-$HOME/druckuebersicht-src}"
ZWEIG="${ZWEIG:-main}"
PORT="${PORT:-8000}"
DIENST="bambuddy"
COMPOSE_DATEI="docker-compose.yml"
ZEIT="$(date +%Y-%m-%d_%H%M)"

gruen() { printf '\033[32m%s\033[0m\n' "$*"; }
gelb() { printf '\033[33m%s\033[0m\n' "$*"; }
fehler() {
    printf '\033[31mFEHLER: %s\033[0m\n' "$*" >&2
    exit 1
}
schritt() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

compose() { docker compose "$@"; }

# Ersetzt beim Dienst "bambuddy" die image-Zeile durch das eigene Image,
# setzt pull_policy: never und entfernt "build: .". Gibt die Anzahl der
# ersetzten image-Zeilen auf stderr aus (fuer die Pruefung).
compose_anpassen() {
    local ein="$1" aus="$2"
    awk -v image="$IMAGE" -v dienst="$DIENST" '
        function einrueckung(s) { match(s, /^[ ]*/); return RLENGTH }
        {
            zeile = $0
            leer = (zeile ~ /^[ ]*$/ || zeile ~ /^[ ]*#/)
            if (!leer && drin && einrueckung(zeile) <= dienst_einr) { drin = 0 }
            if (zeile ~ ("^[ ]+" dienst ":[ ]*$")) { drin = 1; dienst_einr = einrueckung(zeile); print zeile; next }
            if (drin && zeile ~ /^[ ]+image:[ ]*/) {
                pre = substr(zeile, 1, einrueckung(zeile))
                print pre "image: " image
                print pre "pull_policy: never"
                ersetzt++
                next
            }
            if (drin && (zeile ~ /^[ ]+build:[ ]*\.?[ ]*$/ || zeile ~ /^[ ]+pull_policy:/)) { next }
            print zeile
        }
        END { print ersetzt + 0 > "/dev/stderr" }
    ' "$ein" >"$aus"
}

if [[ "${1:-}" == "--nur-compose-test" ]]; then
    # Fuer Tests: nur die Anpassung der docker-compose.yml ausfuehren.
    compose_anpassen "$2" "$3"
    exit 0
fi

schritt "1/4 Pruefungen"
command -v docker >/dev/null || fehler "docker nicht gefunden."
docker compose version >/dev/null 2>&1 || fehler "\"docker compose\" (v2) nicht gefunden."
command -v git >/dev/null || fehler "git nicht gefunden (z. B. sudo apt install git)."
[[ -f "$COMPOSE_DATEI" ]] || fehler "Keine $COMPOSE_DATEI im aktuellen Ordner ($PWD). Bitte in den Ordner der bisherigen Installation wechseln."
grep -Eq "^[ ]+${DIENST}:[ ]*$" "$COMPOSE_DATEI" || fehler "Dienst \"$DIENST\" nicht in $COMPOSE_DATEI gefunden."

CONTAINER="$(compose ps -aq "$DIENST" 2>/dev/null | head -n1)"
[[ -n "$CONTAINER" ]] || fehler "Kein Container fuer \"$DIENST\" gefunden - laeuft Bambuddy aus diesem Ordner?"
DATEN="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{if .Name}}{{.Name}}{{else}}{{.Source}}{{end}}{{end}}{{end}}' "$CONTAINER")"
[[ -n "$DATEN" ]] || fehler "Datenverzeichnis /app/data des Containers nicht gefunden."
echo "Compose-Ordner:  $PWD"
echo "Daten:           $DATEN"
echo "Quellcode:       $QUELLE ($ZWEIG)"

# Freier Platz dort, wo Quellcode und Docker-Daten landen (was davon existiert).
pfade=()
for p in "$(dirname "$QUELLE")" /var/lib/docker; do
    [[ -d "$p" ]] && pfade+=("$p")
done
FREI_GB=""
if ((${#pfade[@]})); then
    FREI_GB="$(df -Pk "${pfade[@]}" 2>/dev/null | awk 'NR>1 {print int($4/1024/1024)}' | sort -n | head -n1 || true)"
fi
if [[ -n "$FREI_GB" && "$FREI_GB" -lt 4 ]]; then
    fehler "Nur ${FREI_GB} GB frei - fuer den Bau werden etwa 4 GB gebraucht."
fi

if [[ "${JA:-}" != "1" ]]; then
    echo
    echo "Bambuddy laeuft waehrend des Baus weiter und ist erst fuer Sicherung und"
    echo "Neustart kurz (etwa 1 Minute) nicht erreichbar."
    read -r -p "Weiter? [j/N] " antwort
    [[ "$antwort" =~ ^[jJyY] ]] || fehler "Abgebrochen."
fi

schritt "2/4 Quellcode holen und Image bauen"
if [[ -d "$QUELLE/.git" ]]; then
    git -C "$QUELLE" fetch --quiet origin "$ZWEIG"
    git -C "$QUELLE" checkout --quiet "$ZWEIG"
    git -C "$QUELLE" pull --quiet --ff-only origin "$ZWEIG"
else
    git clone --quiet --branch "$ZWEIG" "$REPO" "$QUELLE"
fi
echo "Stand: $(git -C "$QUELLE" log --oneline -1)"
docker build -t "$IMAGE" "$QUELLE"
gruen "Image $IMAGE gebaut."

schritt "3/4 Sichern und umstellen"
# Scheitert ab hier etwas, alte docker-compose.yml zurueck und das Original
# wieder starten - Bambuddy soll nie gestoppt liegen bleiben.
# shellcheck disable=SC2329  # wird ueber "trap" aufgerufen
wiederherstellen() {
    local status=$?
    if [[ "${UMGESTELLT:-}" != "1" ]]; then
        gelb "Abbruch - stelle den vorherigen Zustand wieder her ..."
        [[ -f "$COMPOSE_DATEI.$ZEIT" ]] && cp "$COMPOSE_DATEI.$ZEIT" "$COMPOSE_DATEI"
        compose up -d "$DIENST" || true
    fi
    exit "$status"
}
trap wiederherstellen EXIT
compose stop "$DIENST"
SICHERUNG="$PWD/bambuddy-daten-$ZEIT.tgz"
docker run --rm -v "$DATEN":/data:ro -v "$PWD":/backup alpine \
    tar czf "/backup/$(basename "$SICHERUNG")" -C /data .
gruen "Daten gesichert: $SICHERUNG ($(du -h "$SICHERUNG" | cut -f1))"

if [[ ! -f "$COMPOSE_DATEI.vorher" ]]; then
    cp "$COMPOSE_DATEI" "$COMPOSE_DATEI.vorher"
fi
cp "$COMPOSE_DATEI" "$COMPOSE_DATEI.$ZEIT"
ERSETZT="$(compose_anpassen "$COMPOSE_DATEI.$ZEIT" "$COMPOSE_DATEI" 2>&1 >/dev/null)"
if [[ "$ERSETZT" != "1" ]]; then
    fehler "image-Zeile beim Dienst \"$DIENST\" nicht eindeutig gefunden - nichts geaendert."
fi
compose config -q || fehler "Angepasste $COMPOSE_DATEI ist ungueltig - nichts geaendert."
gruen "$COMPOSE_DATEI angepasst (Original: $COMPOSE_DATEI.vorher)."

schritt "4/4 Starten und pruefen"
compose up -d "$DIENST"
UMGESTELLT=1
echo -n "Warte auf die Druckuebersicht auf Port $PORT "
for _ in $(seq 1 60); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$PORT/api/v1/lager-autodruck/status" || true)"
    # 200 = laeuft; 401 = laeuft mit Anmeldung. Beides heisst: neue Version aktiv.
    if [[ "$code" == "200" || "$code" == "401" ]]; then
        echo
        gruen "Die Druckuebersicht laeuft: http://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT"
        echo
        echo "Naechste Schritte: Lager-Autodruck -> Einstellungen (Supabase-Werte, Verbindung testen),"
        echo "Regeln anlegen, 'Jetzt pruefen' und die Vorschau ansehen. Autodruck erst danach einschalten."
        echo "Zurueck zum Original: bash druckuebersicht-zurueck.sh"
        exit 0
    fi
    if [[ "$code" == "404" ]]; then
        echo
        fehler "Bambuddy antwortet, aber ohne Lager-Autodruck (404) - laeuft noch das alte Image? 'docker compose logs $DIENST' ansehen."
    fi
    echo -n "."
    sleep 3
done
echo
gelb "Nach 3 Minuten keine Antwort auf Port $PORT."
gelb "Log ansehen: docker compose logs --tail 100 $DIENST"
gelb "Anderer Port? Dann erneut pruefen mit: curl http://localhost:<PORT>/api/v1/lager-autodruck/status"
gelb "Zurueck zum Original: bash druckuebersicht-zurueck.sh"
exit 1
