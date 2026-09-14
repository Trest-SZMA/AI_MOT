#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ежедневный бэкап сервиса бизнес-планов (контейнерный вариант bp-backup.sh):
горячая копия SQLite через .backup + tar вложений в /data/backups, 30 копий."""
import datetime, os, sqlite3, tarfile

DATA = "/data"
OUT = os.path.join(DATA, "backups")
os.makedirs(OUT, exist_ok=True)
stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

src = sqlite3.connect(os.path.join(DATA, "bp.db"))
dst = sqlite3.connect(os.path.join(OUT, f"bp_{stamp}.db"))
src.backup(dst); dst.close(); src.close()

att = os.path.join(DATA, "attachments")
if os.path.isdir(att):
    with tarfile.open(os.path.join(OUT, f"attachments_{stamp}.tar.gz"), "w:gz") as t:
        t.add(att, arcname="attachments")

for prefix in ("bp_", "attachments_"):
    files = sorted((f for f in os.listdir(OUT) if f.startswith(prefix)), reverse=True)
    for old in files[30:]:
        os.remove(os.path.join(OUT, old))
print("backup ok:", stamp)
