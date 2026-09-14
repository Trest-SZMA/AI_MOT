#!/usr/bin/env bash
# Первичная подготовка Docker-развёртывания (запускать из корня репозитория, один раз):
#   .env (пароль PostgreSQL, IP сервера), env/*.env из шаблонов, каталоги data/,
#   самоподписанный сертификат для https bp-service в certs/.
# Ничего не перезаписывает при повторном запуске.
set -euo pipefail
cd "$(dirname "$0")/../.."

HOST_IP=${1:-$(hostname -I 2>/dev/null | awk '{print $1}')}
[ -n "$HOST_IP" ] || { echo "Не удалось определить IP: deploy/docker/setup.sh <IP сервера>"; exit 1; }

if [ ! -f .env ]; then
  PW=$( (command -v openssl >/dev/null && openssl rand -hex 16) || python3 -c "import secrets;print(secrets.token_hex(16))")
  cat > .env <<EOF
# Настройки docker compose (в git не попадает)
HOST_IP=$HOST_IP
TZ=Asia/Yekaterinburg
DATA_ROOT=./data
POSTGRES_PASSWORD=$PW
EOF
  echo "создан .env (HOST_IP=$HOST_IP)"
fi

mkdir -p env
for ex in deploy/env/*.env.example; do
  name=$(basename "$ex" .example)
  [ "$name" = metoptorg-healthcheck.env ] && name=monitor.env
  [ -f "env/$name" ] || { cp "$ex" "env/$name"; echo "env/$name — заполнить (или import-data.sh)"; }
done
chmod 600 env/*.env

mkdir -p data/{postgres,fines-service,logistmot,metoptorg-ostatki,metoptorg-kp,metoptorg-realizaciya/data,bp-service,parser-bp,metallompro,monitor} certs

if [ ! -f certs/bp-service.crt ]; then
  cat > certs/openssl.cnf <<CONF
[req]
distinguished_name = dn
x509_extensions = v3
prompt = no
[dn]
C  = RU
O  = MetOptTorg
CN = $(hostname)
[v3]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt
[alt]
DNS.1 = $(hostname)
IP.1  = $HOST_IP
CONF
  if command -v openssl >/dev/null; then
    openssl req -x509 -nodes -newkey rsa:2048 -days 3650 -keyout certs/bp-service.key -out certs/bp-service.crt -config certs/openssl.cnf 2>/dev/null
  else
    docker run --rm -v "$PWD/certs:/certs" alpine/openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
      -keyout /certs/bp-service.key -out /certs/bp-service.crt -config /certs/openssl.cnf
  fi
  chmod 644 certs/bp-service.key certs/bp-service.crt   # читает nginx в контейнере (uid 101)
  echo "сертификат: certs/bp-service.crt (SAN: $(hostname), $HOST_IP)"
fi

echo
echo "Дальше:"
echo "  sudo deploy/docker/import-data.sh /root/ai_mot_data_*.tar.gz   # данные и секреты со старого сервера"
echo "  docker compose up -d --build"
echo "  docker compose ps"
