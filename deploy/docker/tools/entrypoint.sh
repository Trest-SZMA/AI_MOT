#!/bin/sh
# Перед стартом создаёт в томе /data каталоги, на которые указывают симлинки
# (файлы вроде bp.db сервис создаёт сам). Затем запускает команду контейнера.
set -e
mkdir -p /data
for l in $LINKS; do
  case "$l" in
    *.db|*.sqlite|*.json) : ;;
    *) mkdir -p "/data/$l" ;;
  esac
done
exec "$@"
