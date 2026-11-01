#!/usr/bin/env bash
# Sichert die PostgreSQL-Datenbank als komprimierten Dump und löscht alte Sicherungen.
#
# Umgebungsvariablen (werden zusätzlich aus <projekt>/.env gelesen):
#   DB_NAME, DB_USER, DB_HOST, DB_PORT   Verbindung (Passwort über ~/.pgpass)
#   BACKUP_DIR                           Zielverzeichnis (Standard: <projekt>/backups)
#   BACKUP_RETENTION_DAYS                Aufbewahrung in Tagen (Standard: 30)
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$PROJECT_DIR/.env"
    set +a
fi

BACKUP_DIR="${BACKUP_DIR:-$PROJECT_DIR/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_FILE="$BACKUP_DIR/${DB_NAME:-molo}_${TIMESTAMP}.dump"

mkdir -p "$BACKUP_DIR"
pg_dump -h "${DB_HOST:-localhost}" -p "${DB_PORT:-5432}" -U "${DB_USER:-molo}" \
    -d "${DB_NAME:-molo}" -F c -f "$BACKUP_FILE"
echo "$(date): Backup erstellt: $BACKUP_FILE ($(du -h "$BACKUP_FILE" | cut -f1))"

find "$BACKUP_DIR" -name "*.dump" -mtime +"$RETENTION_DAYS" -delete
