"""Реестр сделок Битрикса и доля лота.

Половина действующих сделок компании делится с партнёром (УВМ): по 257 из
769 БП доля 50 % («Реализация», сентябрь 2026). До 15.09.2026 сервис считал
каждый лот своим целиком, и план выручки по таким сделкам был вдвое выше
факта. Источник доли — выгрузка сделок из Битрикса `DEAL_<дата>.xlsx`
(та же, что читает «Реализация»): колонки «Доля Металлолом», «Доля УВМ»,
«ЮЛ выиграло КП», «Стадия сделки», ключ — «№ в текущем реестре» (номер
запроса: 1570, 1865 …).

Привязка к БП — по номеру запроса в названии источника («1570_…»,
«КП 1570, перечень 85») или в номере перечня. Доля, заданная в шапке
вручную, импортом не перетирается — как тип сделки.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

DEAL = "deal"
MANUAL = "manual"
_NUM = re.compile(r"(\d+(?:[.,]\d+)?)")
# Четырёхзначный номер запроса, не часть большего числа («1865_Лукойл», «КП 1570»).
_REQ_NO = re.compile(r"(?<!\d)(1[0-9]{3})(?!\d)")


def newest(folder: str | Path) -> Path | None:
    """Самый свежий DEAL_*.xlsx в папке (сортировка по имени = по дате)."""
    files = sorted(Path(folder).glob("DEAL_*.xlsx"))
    return files[-1] if files else None


def _share(v) -> tuple[float | None, bool]:
    """Значение доли из реестра → (проценты 0..100 или None, стоял ли «?»).

    В реестре встречаются 0.5, 50, «50%», «50%?», «100%? Советс…» — как и в
    «Реализации», всё, что больше 1, считаем процентами.
    """
    if v is None or v == "":
        return None, False
    if isinstance(v, (int, float)):
        x = float(v)
        return (x if x > 1 else x * 100.0), False
    t = str(v).strip()
    m = _NUM.search(t)
    if not m:
        return None, "?" in t
    x = float(m.group(1).replace(",", "."))
    if x <= 1 and "%" not in t:
        x *= 100.0
    return x, ("?" in t)


def _num(v) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = _NUM.search(str(v))
    return float(m.group(1).replace(",", ".")) if m else None


def _text(v) -> str | None:
    t = str(v).strip() if v is not None else ""
    return t or None


def load_file(path: str | Path) -> tuple[list[dict], dict]:
    """Строки реестра → записи по номеру запроса + статистика разбора."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = ws.iter_rows(values_only=True)
    hdr = [str(h or "").strip().lower() for h in next(rows)]

    def col(*keys):
        for i, h in enumerate(hdr):
            if all(k in h for k in keys):
                return i
        return None

    c = {"no": col("№ в текущем"), "share": col("доля металлолом"), "uvm": col("доля увм"),
         "winner": col("юл выиграло"), "stage": col("стадия"), "lot": col("объем лота"),
         "contract": col("объем по договору"), "name": col("название сделки"),
         "kind": col("тип"), "owner": col("ответственный"), "created": col("дата создания")}
    if c["no"] is None:
        raise ValueError("в файле сделок нет колонки «№ в текущем реестре»")

    def get(r, key):
        i = c.get(key)
        return r[i] if i is not None and i < len(r) else None

    out: dict[str, dict] = {}
    st = {"rows": 0, "with_no": 0, "with_share": 0, "dup": 0}
    for r in rows:
        st["rows"] += 1
        no = get(r, "no")
        if no is None or str(no).strip() == "":
            continue
        no = str(int(no)) if isinstance(no, (int, float)) else str(no).strip()
        st["with_no"] += 1
        share, doubt = _share(get(r, "share"))
        partner, _ = _share(get(r, "uvm"))
        created = get(r, "created")
        rec = {"deal_no": no, "name": (_text(get(r, "name")) or "")[:120],
               "kind": _text(get(r, "kind")), "stage": _text(get(r, "stage")),
               "share_pct": share, "share_doubt": int(doubt), "partner_pct": partner,
               "winner": _text(get(r, "winner")), "lot_t": _num(get(r, "lot")),
               "contract_t": _num(get(r, "contract")), "owner": _text(get(r, "owner")),
               "created_at": (str(created)[:19] if created is not None else None)}
        if share is not None:
            st["with_share"] += 1
        if no in out:
            st["dup"] += 1
            # Один номер дважды (сделка закрыта и заведена заново) — берём
            # запись с заполненной долей; при обеих заполненных — более позднюю.
            if out[no]["share_pct"] is not None and share is None:
                continue
        out[no] = rec
    return list(out.values()), st


def import_file(conn: sqlite3.Connection, path: str | Path) -> dict:
    """DEAL_*.xlsx → ref_deals (таблица пересоздаётся: реестр — снимок)."""
    recs, st = load_file(path)
    conn.execute("DELETE FROM ref_deals")
    conn.executemany(
        "INSERT INTO ref_deals (deal_no, name, kind, stage, share_pct, share_doubt, "
        "partner_pct, winner, lot_t, contract_t, owner, created_at, source_file, "
        "updated_at) VALUES (:deal_no, :name, :kind, :stage, :share_pct, :share_doubt, "
        ":partner_pct, :winner, :lot_t, :contract_t, :owner, :created_at, :source_file, "
        "datetime('now'))",
        [{**r, "source_file": Path(path).name} for r in recs])
    from . import refsources
    refsources.mark(conn, "ref_deals", len(recs), Path(path).name, "импорт реестра сделок")
    st["written"] = len(recs)
    return st


def request_no(bp) -> str | None:
    """Номер запроса сделки из полей БП: явно привязанный, иначе из названия
    источника («1570_…», «КП 1570 …») или номера перечня."""
    for key in ("deal_no", "source_name", "tender_ref", "bp_number"):
        try:
            v = bp[key]
        except (KeyError, IndexError):
            continue
        if not v:
            continue
        if key == "deal_no":
            return str(v).strip()
        if key == "bp_number":
            continue                       # «БП-0024-2026» — не номер запроса
        m = _REQ_NO.search(str(v))
        if m:
            return m.group(1)
    return None


def lookup(conn: sqlite3.Connection, deal_no: str | None) -> sqlite3.Row | None:
    if not deal_no:
        return None
    try:
        return conn.execute("SELECT * FROM ref_deals WHERE deal_no = ?",
                            (deal_no,)).fetchone()
    except sqlite3.Error:
        return None


def apply_auto(conn: sqlite3.Connection, bp_id: int) -> dict:
    """Подтянуть долю из реестра сделок, если она не задана вручную.

    -> {found, deal_no, share_pct, written, kept}: written — записали новую
    долю; kept — в шапке ручное значение, реестр его не трогает.
    """
    bp = conn.execute("SELECT * FROM business_plans WHERE id = ?", (bp_id,)).fetchone()
    if bp is None:
        return {"found": False, "written": False}
    no = request_no(bp)
    deal = lookup(conn, no)
    res = {"found": deal is not None, "deal_no": no, "written": False, "kept": False,
           "share_pct": deal["share_pct"] if deal is not None else None}
    if deal is None:
        return res
    if (bp["lot_share_source"] or "") == MANUAL:
        res["kept"] = True
        # Реквизиты сделки обновляем и при ручной доле — стадия и юрлицо
        # меняются, а расчёт они не трогают.
        conn.execute("UPDATE business_plans SET deal_no = ?, deal_partner_pct = ?, "
                     "deal_winner = ?, deal_stage = ? WHERE id = ?",
                     (no, deal["partner_pct"], deal["winner"], deal["stage"], bp_id))
        return res
    changed = (bp["lot_share_pct"] != deal["share_pct"]
               or (bp["lot_share_source"] or "") != DEAL or bp["deal_no"] != no)
    conn.execute("UPDATE business_plans SET lot_share_pct = ?, lot_share_source = ?, "
                 "deal_no = ?, deal_partner_pct = ?, deal_winner = ?, deal_stage = ? "
                 "WHERE id = ?",
                 (deal["share_pct"], DEAL, no, deal["partner_pct"], deal["winner"],
                  deal["stage"], bp_id))
    res["written"] = changed
    return res


def apply_all(conn: sqlite3.Connection) -> dict:
    """Все БП реестра (кроме архива): доля из сделок. -> {checked, found, written}."""
    st = {"checked": 0, "found": 0, "written": 0}
    for r in conn.execute("SELECT id FROM business_plans WHERE COALESCE(archived, 0) = 0"):
        res = apply_auto(conn, r["id"])
        st["checked"] += 1
        st["found"] += int(bool(res.get("found")))
        st["written"] += int(bool(res.get("written")))
    return st


def set_manual(conn: sqlite3.Connection, bp_id: int, pct: float | None) -> None:
    """Доля из шапки: число → 'manual'; None → вернуться к реестру сделок."""
    if pct is None:
        conn.execute("UPDATE business_plans SET lot_share_pct = NULL, "
                     "lot_share_source = NULL WHERE id = ?", (bp_id,))
        apply_auto(conn, bp_id)
    else:
        conn.execute("UPDATE business_plans SET lot_share_pct = ?, lot_share_source = ? "
                     "WHERE id = ?", (pct, MANUAL, bp_id))


def describe(conn: sqlite3.Connection, bp) -> dict:
    """Для шапки БП: доля, откуда взята, реквизиты сделки, есть ли реестр."""
    try:
        pct = bp["lot_share_pct"]
        source = bp["lot_share_source"]
    except (KeyError, IndexError):
        pct, source = None, None
    no = request_no(bp)
    deal = lookup(conn, no)
    effective = pct if pct is not None and 0 < float(pct) < 100 else 100.0
    try:
        registry_rows = conn.execute("SELECT COUNT(*) AS n FROM ref_deals").fetchone()["n"]
    except sqlite3.Error:
        registry_rows = 0
    return {"pct": pct, "effective": effective, "source": source, "deal_no": no,
            "deal": deal, "registry_rows": registry_rows,
            "differs": (deal is not None and source == MANUAL
                        and deal["share_pct"] is not None
                        and abs(float(deal["share_pct"]) - float(pct or 100)) > 0.01),
            "doubt": bool(deal is not None and deal["share_doubt"])}
