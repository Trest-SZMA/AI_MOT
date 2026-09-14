#!/usr/bin/env bash
# Ежедневный бэкап: SQLite (горячий, через .backup) + вложения. 30 копий.
set -euo pipefail
DEST=/opt/bp-service
OUT="$DEST/backups"
STAMP=$(date +%Y%m%d_%H%M%S)
mkdir -p "$OUT"
"$DEST/.venv/bin/python" - <<PY
import sqlite3
src=sqlite3.connect("$DEST/bp.db"); dst=sqlite3.connect("$OUT/bp_$STAMP.db")
src.backup(dst); dst.close(); src.close()
PY
tar -czf "$OUT/attachments_$STAMP.tar.gz" -C "$DEST" attachments 2>/dev/null || true
ls -1t "$OUT"/bp_*.db 2>/dev/null | tail -n +31 | xargs -r rm --
ls -1t "$OUT"/attachments_*.tar.gz 2>/dev/null | tail -n +31 | xargs -r rm --
