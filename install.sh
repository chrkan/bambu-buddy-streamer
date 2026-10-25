#!/usr/bin/env bash
# bambu-buddy-streamer – Installer / Updater für einen Debian-LXC (als root ausführen)
#
#   Installation / Update:
#     bash <(curl -fsSL https://raw.githubusercontent.com/chrkan/bambu-buddy-streamer/main/install.sh)
#
#   Optionen:
#     --local            Dateien neben diesem Skript verwenden statt von GitHub zu laden
#     --uninstall        Dienste und Programmdateien entfernen (Config bleibt)
#     --uninstall --purge  zusätzlich Config und Passwort löschen
#   Umgebungsvariablen:
#     BRANCH=name        anderen Branch laden (Standard: main)
set -euo pipefail

REPO="chrkan/bambu-buddy-streamer"
BRANCH="${BRANCH:-main}"
RAW_BASE="${RAW_BASE:-https://raw.githubusercontent.com/$REPO/$BRANCH}"
INSTALL_DIR=/opt/bambu-twitch
CONF_DIR=/etc/bambu-twitch
WEB_PORT=8080
FILES=(bambu_twitch.py web.py)

[ "$(id -u)" -eq 0 ] || { echo "Bitte als root ausführen." >&2; exit 1; }

LOCAL=0
UNINSTALL=0
PURGE=0
for arg in "$@"; do
  case "$arg" in
    --local) LOCAL=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --purge) PURGE=1 ;;
    *) echo "Unbekannte Option: $arg" >&2; exit 1 ;;
  esac
done

if [ "$UNINSTALL" -eq 1 ]; then
  systemctl disable --now bambu-twitch bambu-twitch-web 2>/dev/null || true
  rm -f /etc/systemd/system/bambu-twitch.service /etc/systemd/system/bambu-twitch-web.service
  rm -rf /etc/systemd/system/bambu-twitch.service.d "$INSTALL_DIR"
  [ "$PURGE" -eq 1 ] && rm -rf "$CONF_DIR"
  systemctl daemon-reload
  echo "Entfernt. $([ "$PURGE" -eq 1 ] || echo "Die Config in $CONF_DIR wurde behalten (--purge löscht sie).")"
  exit 0
fi

apt-get update
apt-get install -y ffmpeg python3 python3-venv python3-pip curl ca-certificates

mkdir -p "$INSTALL_DIR" "$CONF_DIR"
chmod 700 "$CONF_DIR"

# ---- Programmdateien beschaffen (GitHub oder lokal) ----
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
for f in "${FILES[@]}"; do
  tmp="$(mktemp)"
  if [ "$LOCAL" -eq 1 ]; then
    [ -f "$SRC_DIR/$f" ] || { echo "Lokale Datei fehlt: $SRC_DIR/$f" >&2; exit 1; }
    cp "$SRC_DIR/$f" "$tmp"
  else
    echo "Lade $f von GitHub ($BRANCH) …"
    curl -fsSL --retry 3 "$RAW_BASE/$f" -o "$tmp" \
      || { echo "Download fehlgeschlagen: $RAW_BASE/$f" >&2; rm -f "$tmp"; exit 1; }
  fi
  [ -s "$tmp" ] || { echo "Datei ist leer: $f" >&2; rm -f "$tmp"; exit 1; }
  python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' "$tmp" \
    || { echo "Datei ist kein gültiges Python: $f" >&2; rm -f "$tmp"; exit 1; }
  install -m 644 "$tmp" "$INSTALL_DIR/$f"
  rm -f "$tmp"
done

# ---- Python-Umgebung ----
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip playwright flask waitress
# Chromium + System-Bibliotheken
"$INSTALL_DIR/venv/bin/playwright" install --with-deps chromium

# ---- Config (nur beim ersten Mal anlegen) ----
if [ ! -f "$CONF_DIR/config.env" ]; then
cat > "$CONF_DIR/config.env" <<'CONF'
BAMBUDDY_CAMERA_URL=""
BAMBUDDY_OVERLAY_URL=""
OVERLAY_CSS=""
TWITCH_ENABLED="1"
TWITCH_STREAM_KEY=""
YOUTUBE_ENABLED="1"
YOUTUBE_STREAM_KEY=""
WIDTH="1280"
HEIGHT="720"
FPS="15"
OVERLAY_FPS="5"
VIDEO_BITRATE="3500k"
X264_PRESET="veryfast"
SILENT_AUDIO="1"
CONF
chmod 600 "$CONF_DIR/config.env"
fi

# ---- systemd ----
cat > /etc/systemd/system/bambu-twitch.service <<UNIT
[Unit]
Description=Bambu Kamera + Overlay -> Twitch / YouTube
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=$CONF_DIR/config.env
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/bambu_twitch.py
Restart=always
RestartSec=10
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/bambu-twitch-web.service <<UNIT
[Unit]
Description=Bambu-Twitch Webinterface
After=network-online.target
Wants=network-online.target

[Service]
Environment=BAMBU_WEB_PORT=$WEB_PORT
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/web.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

# Initialpasswort erzeugen (nur, wenn noch keins existiert)
INIT_PW="$("$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/web.py" --init)"

systemctl daemon-reload
systemctl enable --now bambu-twitch-web
systemctl restart bambu-twitch-web

IP="$(hostname -I | awk '{print $1}')"
echo
echo "=============================================================="
echo " Installation abgeschlossen"
echo " Webinterface:  http://${IP}:${WEB_PORT}"
if [ -n "$INIT_PW" ]; then
  echo " Initialpasswort: ${INIT_PW}"
  echo " (wird nur jetzt angezeigt, beim ersten Login änderst du es)"
else
  echo " Es existiert bereits ein Passwort. Neues Initialpasswort mit:"
  echo "   $INSTALL_DIR/venv/bin/python $INSTALL_DIR/web.py --reset-password"
fi
if systemctl is-active --quiet bambu-twitch; then
  echo
  echo " Hinweis: Der Stream läuft noch mit der alten Version."
  echo " Im Webinterface auf „Neustart“ klicken, um die neue zu laden."
fi
echo "=============================================================="
