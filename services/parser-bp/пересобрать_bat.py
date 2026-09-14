# -*- coding: utf-8 -*-
"""
Пересборка .bat-файлов из папки bat_src.

ЗАЧЕМ: cmd.exe требует переводы строк CRLF и кодировку консоли cp866.
Если править .bat напрямую в macOS-редакторе, файл сохранится с LF и в UTF-8 —
Windows тогда не находит метки goto и окно закрывается сразу после открытия,
не показав ни одной ошибки.

Правим исходники в bat_src/ (обычный UTF-8), затем:

    python3 пересобрать_bat.py
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).parent
SRC_DIR = ROOT / "bat_src"


def check_labels(text: str, name: str) -> list[str]:
    """Все ли цели goto имеют метки — самая частая причина молчаливого выхода."""
    labels = {line.strip()[1:] for line in text.splitlines() if re.match(r"^:[A-Za-z_]", line.strip())}
    targets = set(re.findall(r"\bgoto\s+:?([A-Za-z_]\w*)", text))
    return sorted(targets - labels - {"eof"})


def main() -> int:
    if not SRC_DIR.is_dir():
        print(f"Нет папки с исходниками: {SRC_DIR}")
        return 1

    errors = 0
    for src in sorted(SRC_DIR.glob("*.bat")):
        text = src.read_text(encoding="utf-8")

        missing = check_labels(text, src.name)
        if missing:
            print(f"[ОШИБКА] {src.name}: goto без метки — {', '.join(missing)}")
            errors += 1
            continue

        crlf = text.replace("\r\n", "\n").replace("\n", "\r\n")
        try:
            data = crlf.encode("cp866")
        except UnicodeEncodeError as exc:
            bad = crlf[exc.start : exc.end]
            print(f"[ОШИБКА] {src.name}: символ {bad!r} не кодируется в cp866 — замените его")
            errors += 1
            continue

        (ROOT / src.name).write_bytes(data)
        print(f"{src.name}: {len(data)} байт, CRLF, cp866 — готово")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
