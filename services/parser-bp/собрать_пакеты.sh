#!/bin/bash
# Пересборка папки packages — офлайн-комплект библиотек для Windows.
#
# Запускать на машине С ИНТЕРНЕТОМ (можно с macOS — колёса скачиваются
# под Windows кросс-платформенно).
#
#   ./собрать_пакеты.sh              # версии Python 3.11 3.12 3.13 3.14
#   ./собрать_пакеты.sh 3.15         # только указанные версии

set -e
cd "$(dirname "$0")"

VERSIONS=("$@")
if [ ${#VERSIONS[@]} -eq 0 ]; then
    VERSIONS=(3.11 3.12 3.13 3.14)
fi

echo "Собираю пакеты для Windows x64, Python: ${VERSIONS[*]}"
mkdir -p packages

for V in "${VERSIONS[@]}"; do
    echo "--- Python $V"
    python3 -m pip download \
        --only-binary=:all: \
        --platform win_amd64 \
        --python-version "$V" \
        -d packages \
        -r requirements.txt
done

echo
echo "Готово: $(ls packages | wc -l | tr -d ' ') файлов, $(du -sh packages | cut -f1)"
