#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_dash_data.py — тонкая обёртка: выполнить расчёт и получить out/dashboard_data.json.
Идентично `python3 stock_report.py`. Оставлено для совместимости с эксплуатацией.
"""
import runpy, os, sys
sys.argv = [sys.argv[0]] + sys.argv[1:]
runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "stock_report.py"), run_name="__main__")
