# -*- coding: utf-8 -*-
"""
Системный диалог выбора папки.

Запускается ОТДЕЛЬНЫМ процессом (см. app.py), потому что Tk нельзя дёргать
из рабочего потока Streamlit — на macOS это роняет процесс целиком.
Выбранный путь печатается в stdout; при отмене печатается пустая строка.
"""

from __future__ import annotations

import sys


def ask_directory(initial: str = "") -> str:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)   # чтобы окно не ушло за браузер
    except Exception:  # noqa: BLE001
        pass
    root.update()
    path = filedialog.askdirectory(
        title="Выберите папку с бизнес-планами",
        initialdir=initial or None,
        mustexist=True,
    )
    root.destroy()
    return path or ""


if __name__ == "__main__":
    initial_dir = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        sys.stdout.write(ask_directory(initial_dir))
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(str(exc))
        raise SystemExit(2)
