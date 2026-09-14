#!/bin/bash
# Запуск парсера БП на macOS (двойной щелчок по файлу)
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 не найден. Установите с https://www.python.org/downloads/"
    read -r -p "Нажмите Enter для выхода"
    exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
    echo "Первый запуск: создаю окружение и ставлю библиотеки…"
    python3 -m venv .venv || exit 1
    .venv/bin/python -m pip install --upgrade pip --quiet
    .venv/bin/python -m pip install -r requirements.txt --quiet || exit 1
fi

echo "Запускаю приложение. Откроется вкладка в браузере."
.venv/bin/python -m streamlit run app.py --browser.gatherUsageStats=false
