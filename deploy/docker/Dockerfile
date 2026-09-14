# Общий образ для всех Python-сервисов МетОптТорг.
# Собирается из корня репозитория с аргументами:
#   SERVICE — имя каталога в services/ (и рабочий каталог /opt/<SERVICE>, как на сервере)
#   LINKS   — что из каталога сервиса живёт в томе /data (через симлинки), например
#             "bp.db attachments 1c output backups" — так код с относительными путями
#             работает без изменений, а данные лежат в примонтированном томе.
FROM python:3.12-slim

ARG SERVICE
ARG LINKS=""
ENV SERVICE=$SERVICE LINKS=$LINKS \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Yekaterinburg

RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends curl poppler-utils tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/$SERVICE
COPY services/$SERVICE/ /opt/$SERVICE/
COPY deploy/docker/tools/ /tools/

RUN if [ -f requirements.txt ]; then pip install -q -r requirements.txt; fi \
 && chmod +x /tools/*.sh \
 && for l in $LINKS; do rm -rf "/opt/$SERVICE/$l"; ln -s "/data/$l" "/opt/$SERVICE/$l"; done

VOLUME /data
ENTRYPOINT ["/tools/entrypoint.sh"]
