"""Полная загрузка dev-базы из реальных файлов data/samples."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import init_db, SessionLocal, engine  # noqa: E402
from app.db.models import Base  # noqa: E402
from app.seed import seed  # noqa: E402
from app.importers.nomenclature import import_nomenclature  # noqa: E402
from app.importers.cable_files import (  # noqa: E402
    import_brand_table, import_valuation_sheet, import_vnpz_sheet)
from app.importers.metal_yields import import_yields_file, rebuild_profiles  # noqa: E402
from app.importers.kp import import_kp  # noqa: E402

S = Path(__file__).resolve().parents[1] / "data" / "samples"


def main(fresh: bool = True):
    if fresh:
        Base.metadata.drop_all(engine)
    init_db()
    s = SessionLocal()
    seed(s)

    t0 = time.time()
    st = import_nomenclature(s, str(S / "spravochniki.xlsx"))
    print(f"номенклатура: {st.rows} строк, {st.created} новых, "
          f"{st.merged_duplicates} дублей слито, {st.trade} торговых "
          f"({time.time()-t0:.1f}s)")

    st = import_valuation_sheet(s, str(S / "ocenka_metalla.xlsx"),
                                "Оценка металла", "Оценка_металла")
    print(f"Оценка_металла: {st.imported} строк, Σ {st.checks['metal_kg_sum']} кг")
    st = import_valuation_sheet(s, str(S / "kabel_normy.xlsx"),
                                "ННОС 29.12.25", "ННОС 29.12.25")
    print(f"ННОС: {st.imported} строк, Σ {st.checks['metal_kg_sum']} кг")
    st = import_valuation_sheet(s, str(S / "kabel_normy.xlsx"),
                                "ПНОС кабель 12.25", "ПНОС 12.25")
    print(f"ПНОС: {st.imported} строк, Σ {st.checks['metal_kg_sum']} кг")
    st = import_brand_table(s, str(S / "kabel_normy.xlsx"),
                            "Таблица по маркам", "Таблица по маркам")
    print(f"Таблица по маркам: {st.imported} марок ({len(st.skipped)} пропущено)")
    st = import_vnpz_sheet(s, str(S / "kabel_normy.xlsx"), "ВНПЗ медь",
                           "медь", "ВНПЗ медь")
    print(f"ВНПЗ медь: {st.imported} марок")
    st = import_vnpz_sheet(s, str(S / "kabel_normy.xlsx"), "ВНПЗ алюминий",
                           "алюминий", "ВНПЗ алюминий")
    print(f"ВНПЗ алюминий: {st.imported} марок")

    for f, src in [("vyhody_41_51.xlsx", "выходы 41-51"),
                   ("vyhody_25_28.xlsx", "выходы 25-28"),
                   ("vyhody_39_40.xlsx", "выходы 39-40")]:
        st = import_yields_file(s, str(S / f), src)
        print(f"{src}: {st.batches} партий, {st.yields} выходов, "
              f"перечни {st.lists}")
    print("профили семейств:", rebuild_profiles(s))

    t0 = time.time()
    doc, res = import_kp(s, str(S / "vyhody_39_40.xlsx"), "Демо-КП (39-40).xlsx")
    print(f"демо-КП: {res.positions} позиций из {res.lots} лотов "
          f"({time.time()-t0:.1f}s)")
    s.close()


if __name__ == "__main__":
    main(fresh="--keep" not in sys.argv)
