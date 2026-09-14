# Реестр сервисов. Подключается скриптами deploy/*.sh, сам не запускается.
#
# Формат строки:  имя|пользователь|venv|юниты systemd (через пробел)
#   имя           — каталог services/<имя> в репозитории и /opt/<имя> на сервере
#   пользователь  — под кем работает служба (root = без User= или DynamicUser)
#   venv          — yes: нужен .venv из requirements.txt; no: системный python3
#   юниты         — что включать/перезапускать (service и timer)
SERVICES=(
  "metoptorg-portal|root|no|metoptorg-portal.service"
  "fines-service|fines|yes|fines-service.service"
  "logistmot|logistmot|yes|logistmot-bot.service logistmot-panel.service"
  "metoptorg-ostatki|root|yes|metoptorg-ostatki.service"
  "metoptorg-kp|root|yes|metoptorg-kp.service metoptorg-kp-backup.timer"
  "metoptorg-realizaciya|metoptorg|yes|metoptorg-realizaciya.service metoptorg-realizaciya-nightly.timer"
  "parser-bp|root|yes|parser-bp.service metoptorg-bp-weekly.timer"
  "bp-service|bpservice|yes|bp-service.service"
  "metallompro|root|yes|metallompro.service"
)

# HTTP-порты для проверки (портал отвечает 401 без пароля — это норма).
declare -A PORTS=(
  [fines-service]=8077 [metoptorg-portal]=8079 [logistmot]=8080
  [metoptorg-ostatki]=8090 [metoptorg-kp]=8091 [metoptorg-realizaciya]=8092
  [bp-service]=8093 [parser-bp]=8094 [metallompro]=8095
)

# Где лежит env-файл с секретами каждого сервиса.
declare -A ENV_FILE=(
  [metoptorg-portal]=/etc/metoptorg-portal.env
  [logistmot]=/opt/logistmot/.env
  [metoptorg-ostatki]=/etc/metoptorg-ostatki.env
  [metoptorg-kp]=/etc/metoptorg-kp.env
  [metoptorg-realizaciya]=/etc/metoptorg-realizaciya.env
  [parser-bp]=/etc/parser-bp.env
  [bp-service]=/etc/bp-service.env
  [metallompro]=/etc/metallompro.env
)

svc_field() {  # svc_field <имя> <номер поля 2..4>
  local name=$1 n=$2 row
  for row in "${SERVICES[@]}"; do
    [ "${row%%|*}" = "$name" ] && { echo "$row" | cut -d'|' -f"$n"; return; }
  done
  return 1
}

svc_names() { local r; for r in "${SERVICES[@]}"; do echo "${r%%|*}"; done; }

log()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!!  %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mОШИБКА: %s\033[0m\n' "$*" >&2; exit 1; }
