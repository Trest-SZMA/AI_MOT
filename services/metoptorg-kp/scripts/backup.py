#!/usr/bin/env python3
"""Ежесуточный бэкап сервиса: база + загруженные исходные файлы.

База копируется онлайн через sqlite3.backup() — обычный `cp` работающей базы
может дать битый файл, если в этот момент идёт запись. Исходные файлы КП и
выгрузок — юридический след оценки, поэтому сохраняются вместе с базой.

Хранится 14 последних копий. Запускается systemd-таймером от root.
"""
from __future__ import annotations

import datetime as dt
import gzip
import os
import shutil
import sqlite3
import sys
import tarfile
from pathlib import Path

STATE_DIR = Path(os.environ.get("METOPTTORG_DATA_DIR",
                                "/var/lib/private/metoptorg-kp"))
BACKUP_DIR = Path(os.environ.get("METOPTTORG_BACKUP_DIR",
                                 "/var/backups/metoptorg-kp"))
KEEP = int(os.environ.get("METOPTTORG_BACKUP_KEEP", "14"))


def backup_database(stamp: str) -> Path:
    src = STATE_DIR / "metopttorg.db"
    if not src.exists():
        sys.exit(f"база не найдена: {src}")
    tmp = BACKUP_DIR / f".tmp_{stamp}.db"
    with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as source, \
            sqlite3.connect(tmp) as target:
        source.backup(target)  # согласованная копия даже при активной записи
    out = BACKUP_DIR / f"metopttorg_{stamp}.db.gz"
    with open(tmp, "rb") as f_in, gzip.open(out, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out)
    tmp.unlink()
    return out


def backup_uploads(stamp: str) -> Path | None:
    uploads = STATE_DIR / "uploads"
    if not uploads.exists() or not any(uploads.iterdir()):
        return None
    out = BACKUP_DIR / f"uploads_{stamp}.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        tar.add(uploads, arcname="uploads")
    return out


def rotate(prefix: str) -> int:
    files = sorted(BACKUP_DIR.glob(f"{prefix}_*"), reverse=True)
    removed = 0
    for old in files[KEEP:]:
        old.unlink()
        removed += 1
    return removed


def verify(db_gz: Path) -> str:
    """Проверяем, что копия восстанавливается и содержит данные."""
    tmp = BACKUP_DIR / ".verify.db"
    with gzip.open(db_gz, "rb") as f_in, open(tmp, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    try:
        con = sqlite3.connect(tmp)
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        items = con.execute("SELECT count(*) FROM items").fetchone()[0]
        kp = con.execute("SELECT count(*) FROM kp_documents").fetchone()[0]
        con.close()
        return f"проверка: {ok}, позиций справочника {items}, КП {kp}"
    finally:
        tmp.unlink(missing_ok=True)


def main() -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
    db = backup_database(stamp)
    uploads = backup_uploads(stamp)
    print(f"база: {db.name} ({db.stat().st_size / 1e6:.1f} МБ) — {verify(db)}")
    if uploads:
        print(f"файлы: {uploads.name} ({uploads.stat().st_size / 1e6:.1f} МБ)")
    removed = rotate("metopttorg") + rotate("uploads")
    print(f"хранится копий: {KEEP}, удалено старых: {removed}")


if __name__ == "__main__":
    main()
