#!/usr/bin/env bash
# Führt einen manage.py-Befehl mit der virtuellen Umgebung des Projekts aus.
# Gedacht für Cron-Jobs und systemd-Timer, siehe deploy/crontab.example.
#
#   scripts/manage.sh import_articles
#
# Umgebungsvariablen:
#   MOLO_VENV               Pfad zur virtuellen Umgebung (Standard: <projekt>/.venv)
#   DJANGO_SETTINGS_MODULE  Standard: molo.settings.production
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${MOLO_VENV:-$PROJECT_DIR/.venv}"

cd "$PROJECT_DIR"
export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-molo.settings.production}"
exec "$VENV/bin/python" manage.py "$@"
