#!/usr/bin/env bash
# Проверка: все ли службы активны и отвечают ли порты.
#   deploy/check.sh
set -uo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
source "$REPO/deploy/services.sh"

fail=0
printf '%-24s %-10s %-6s %s\n' "СЕРВИС" "ЮНИТ" "ПОРТ" "HTTP"
for name in $(svc_names); do
  for unit in $(svc_field "$name" 4); do
    case $unit in *.timer) continue;; esac
    st=$(systemctl is-active "$unit" 2>/dev/null)
    port=${PORTS[$name]:-}
    code=""
    if [ -n "$port" ] && [[ "$unit" != logistmot-bot* ]]; then
      code=$(curl -s -o /dev/null -w '%{http_code}' -m 8 "http://127.0.0.1:$port/" || echo 000)
      [ "$code" = 000 ] && fail=1
    fi
    [ "$st" = active ] || fail=1
    printf '%-24s %-10s %-6s %s\n' "$unit" "$st" "${port:-—}" "${code:-—}"
  done
done
code=$(curl -sk -o /dev/null -w '%{http_code}' -m 8 https://127.0.0.1:8443/ || echo 000)
printf '%-24s %-10s %-6s %s\n' "nginx (https bp)" "$(systemctl is-active nginx)" 8443 "$code"
printf '%-24s %-10s\n' "postgresql" "$(systemctl is-active postgresql)"

echo; echo "Таймеры:"; systemctl list-timers --no-pager | grep -E 'metoptorg|NEXT' || true
echo; df -h / | tail -1
[ $fail = 0 ] && echo -e "\nВСЁ В ПОРЯДКЕ" || { echo -e "\nЕСТЬ ПРОБЛЕМЫ — journalctl -u <юнит> -n 50"; exit 1; }
