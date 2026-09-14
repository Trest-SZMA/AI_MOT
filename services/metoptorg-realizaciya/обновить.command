#!/bin/bash
# Пересборка отчёта реализации: расчёт -> дашборд -> Excel -> проверки.
# Двойной щелчок в Finder или ./обновить.command
cd "$(dirname "$0")" || exit 1

# Воспроизводимость: без фиксированного seed две сборки на одних и тех же
# выгрузках дают чуть разный результат (см. deploy/metoptorg-realizaciya.service).
export PYTHONHASHSEED=0

echo "=============================================="
echo " МЕТОПТОРГ · пересборка отчёта по реализации"
echo "=============================================="

python3 sales_report.py || { echo "!! расчёт не прошёл"; read -r -p "Enter…"; exit 1; }
echo
python3 make_dash.py    || { echo "!! дашборд не собрался"; read -r -p "Enter…"; exit 1; }
python3 make_excel.py   || { echo "!! Excel не собрался";   read -r -p "Enter…"; exit 1; }

echo
echo "---------- контрольные примеры ТЗ §12 ----------"
python3 tools/control_check.py | tail -n 24
echo
echo "---------- выгрузка в 1С ----------"
python3 tools/check_1c_xlsx.py 2>/dev/null | tail -n 4 || echo "(пропущено: нет node)"

echo
echo "Готово. Файлы в out/:"
ls -lh out/*.html out/*.xlsx 2>/dev/null | awk '{print "  " $9 "  " $5}'
read -r -p "Enter, чтобы закрыть…"
