#!/usr/bin/env bash
# Самоподписанный сертификат для HTTPS сервиса бизнес-планов.
#
# Публичного имени у сервера нет (домен ui.local), поэтому Let's Encrypt
# сертификат не выдаст. Имя и IP кладутся в SAN: браузеры с 2017 года
# смотрят именно туда, а не в CN, и без IP в SAN вход по адресу
# https://192.168.6.157:8443 давал бы ошибку имени.
#
# Запуск на сервере от root:  ./make-cert.sh [имя] [ip]
set -euo pipefail

NAME="${1:-ubuntutest.ui.local}"
IP="${2:-192.168.6.157}"
DIR=/etc/ssl/bp-service
DAYS=3650                      # внутренний сертификат: меняем вместе с сервером

mkdir -p "$DIR"
cat > "$DIR/openssl.cnf" <<CONF
[req]
distinguished_name = dn
x509_extensions = v3
prompt = no

[dn]
C  = RU
O  = MetOptTorg
CN = $NAME

[v3]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt

[alt]
DNS.1 = $NAME
DNS.2 = ${NAME%%.*}
IP.1  = $IP
CONF

openssl req -x509 -nodes -newkey rsa:2048 -days "$DAYS" \
    -keyout "$DIR/bp-service.key" -out "$DIR/bp-service.crt" \
    -config "$DIR/openssl.cnf"

chmod 600 "$DIR/bp-service.key"
chmod 644 "$DIR/bp-service.crt"

echo
echo "Сертификат: $DIR/bp-service.crt"
openssl x509 -in "$DIR/bp-service.crt" -noout -subject -dates -ext subjectAltName
echo
echo "Чтобы браузеры не ругались, раздайте файл .crt как доверенный"
echo "корневой на компьютеры сотрудников (в домене — групповой политикой)."
