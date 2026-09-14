#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_dash.py — собирает самодостаточный офлайн-HTML дашборд:
   web/app_template.html + web/tabs.js + out/dashboard_data.json -> out/dashboard.html
"""
import os, json, sys
ROOT = os.path.dirname(os.path.abspath(__file__))

def main():
    tpl = open(os.path.join(ROOT, "web", "app_template.html"), encoding="utf-8").read()
    tabs = open(os.path.join(ROOT, "web", "tabs.js"), encoding="utf-8").read()
    data = open(os.path.join(ROOT, "out", "dashboard_data.json"), encoding="utf-8").read()
    # встраиваем данные
    a = tpl.index("/*__DATA__*/"); b = tpl.index("/*__END__*/") + len("/*__END__*/")
    html = tpl[:a] + data + tpl[b:]
    # встраиваем JS вкладок
    html = html.replace("<!--__TABS_JS__-->", "<script>\n" + tabs + "\n</script>")
    out = os.path.join(ROOT, "out", "dashboard.html")
    open(out, "w", encoding="utf-8").write(html)
    print("Готово: %s (%.1f МБ)" % (out, os.path.getsize(out)/1e6))

if __name__ == "__main__":
    main()
