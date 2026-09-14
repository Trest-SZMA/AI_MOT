# -*- coding: utf-8 -*-
"""
Парсер БП — проверка количества месяцев вывоза.

Запуск:  streamlit run app.py
(или двойным щелчком по ЗАПУСТИТЬ.bat в Windows / run.command в macOS)
"""

from __future__ import annotations

import datetime as dt
import os
import string
import subprocess
import sys

import pandas as pd
import streamlit as st

from export import build_workbook
from bitrix_link import scan_bitrix_link
from parser_core import scan_folder, scan_uploads

# На сервере включается пароль и режим загрузки файлов; локально в Windows
# обе переменные не заданы и всё работает как раньше.
APP_PASSWORD = os.environ.get("PARSER_BP_PASSWORD", "")
SERVER_MODE = os.environ.get("PARSER_BP_SERVER", "") == "1"
# Адрес портала для кнопки «назад». Пусто = кнопки нет (локальный запуск в Windows).
PORTAL_URL = os.environ.get(
    "PARSER_BP_PORTAL_URL", "http://192.168.6.157:8079" if SERVER_MODE else ""
)

_FAVICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "favicon.svg")
st.set_page_config(page_title="Парсер БП — проверка месяцев",
                   page_icon=_FAVICON if os.path.exists(_FAVICON) else "📊", layout="wide")

COLORS = {
    "ОК": ("#c6efce", "#006100"),
    "Расхождение": ("#ffc7ce", "#9c0006"),
    "Не заполнен": ("#e4e4e4", "#4d4d4d"),
    "Не определено": ("#ffeb9c", "#9c5700"),
}

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".last_folder.txt")


def load_last_folder() -> str:
    """Последний использованный путь (чтобы не вводить сетевой путь каждый раз)."""
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def save_last_folder(folder: str) -> None:
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
            fh.write(folder)
    except OSError:
        pass  # папка программы только для чтения — не критично


DIALOG_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "folder_dialog.py")


def render_title() -> None:
    """
    Заголовок страницы. На сервере значок слева — ссылка на портал:
    по нему возвращаемся к списку сервисов.
    """
    if PORTAL_URL:
        st.markdown(
            f'<h1 style="margin-bottom:0.2rem">'
            f'<a href="{PORTAL_URL}" target="_self" title="Вернуться на портал" '
            f'style="text-decoration:none">📊</a> '
            f'Парсер БП — проверка количества месяцев вывоза</h1>',
            unsafe_allow_html=True,
        )
    else:
        st.title("📊 Парсер БП — проверка количества месяцев вывоза")


def render_portal_link() -> None:
    """Ссылка на портал в самом верху боковой панели."""
    if PORTAL_URL:
        st.sidebar.markdown(
            f'<a href="{PORTAL_URL}" target="_self" '
            f'style="text-decoration:none;font-size:0.9rem">← На портал</a>',
            unsafe_allow_html=True,
        )


def check_password() -> bool:
    """
    Пускает дальше, если пароль не задан (локальный запуск) или введён верно.

    Пароль лежит в переменной окружения PARSER_BP_PASSWORD и задаётся только
    на сервере — портал сам паролей не хранит, каждый сервис спрашивает свой.
    """
    if not APP_PASSWORD:
        return True
    if st.session_state.get("authed"):
        return True

    if PORTAL_URL:
        st.markdown(
            f'<h1><a href="{PORTAL_URL}" target="_self" title="Вернуться на портал" '
            f'style="text-decoration:none">📊</a> Парсер БП</h1>',
            unsafe_allow_html=True,
        )
    else:
        st.title("📊 Парсер БП")
    st.caption("Проверка количества месяцев вывоза в бизнес-планах")
    with st.form("login"):
        entered = st.text_input("Пароль", type="password")
        submitted = st.form_submit_button("Войти", type="primary")
    if submitted:
        if entered == APP_PASSWORD:
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("Неверный пароль.")
    return False


def dialog_available() -> bool:
    """Системное окно выбора папки имеет смысл только на компьютере пользователя."""
    if SERVER_MODE:
        return False
    try:
        import tkinter  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def pick_folder_dialog(initial: str = "") -> tuple[str, str]:
    """
    Открывает системный диалог выбора папки отдельным процессом.

    Возвращает (путь, ошибка). Пустой путь без ошибки = пользователь нажал «Отмена».
    Отдельный процесс нужен, потому что Tk нельзя вызывать из рабочего потока Streamlit.
    """
    try:
        proc = subprocess.run(
            [sys.executable, DIALOG_SCRIPT, initial],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        return "", "Окно выбора папки не закрыли за 10 минут."
    except OSError as exc:
        return "", f"Не удалось запустить диалог: {exc}"

    if proc.returncode != 0:
        reason = (proc.stderr or "").strip().splitlines()
        detail = reason[-1] if reason else "неизвестная ошибка"
        return "", f"Системный диалог недоступен ({detail}). Воспользуйтесь навигатором ниже."
    return proc.stdout.strip(), ""


def list_subfolders(path: str) -> list[str]:
    """Имена подпапок (скрытые и служебные пропускаются)."""
    try:
        with os.scandir(path) as it:
            names = [
                entry.name
                for entry in it
                if not entry.name.startswith((".", "~$")) and entry.is_dir()
            ]
    except OSError:
        return []
    return sorted(names, key=str.lower)


def windows_drives() -> list[str]:
    """Список доступных дисков в Windows (для быстрого старта навигатора)."""
    if os.name != "nt":
        return []
    return [f"{letter}:\\" for letter in string.ascii_uppercase if os.path.isdir(f"{letter}:\\")]


def set_folder(path: str) -> None:
    """
    Просит подставить путь в поле ввода на следующем проходе.

    Напрямую записать в folder_input нельзя: Streamlit запрещает менять значение
    виджета после того, как он уже отрисован в этом проходе.
    """
    st.session_state["pending_folder"] = path
    st.rerun()


COLUMNS = {
    "bp_name": "Название БП и версия",
    "months_by_rows": "Кол-во месяцев по строкам",
    "months_by_cell": "Кол-во месяцев по ячейке",
    "net_profit": "Чистая прибыль",
    "status": "Статус",
    "mismatch": "Расхождение",
    "has_lukoil": "Лукойл",
    "has_dsp": "ДСП",
    "file_name": "Файл",
    "sheet_name": "Лист",
    "profit_sheet": "Лист с прибылью",
    "block_cell": "Ячейка блока",
    "rows_detail": "Строки блока",
    "file_path": "Полный путь",
    "hidden_sheets": "Скрытые листы",
}


def main():
    if not check_password():
        return

    render_portal_link()
    render_title()
    st.caption(
        "Программа обходит папку с бизнес-планами, находит на листах блок "
        "«Расчет процентов» и сравнивает количество месяцев, посчитанное по строкам "
        "(проставленный порядковый номер месяца), со значением строки «Кол-во месяцев вывоза»."
    )

    if "folder_input" not in st.session_state:
        st.session_state["folder_input"] = load_last_folder()

    # путь, выбранный диалогом или навигатором на прошлом проходе
    pending = st.session_state.pop("pending_folder", None)
    if pending:
        st.session_state["folder_input"] = pending
        st.session_state["browse_dir"] = pending

    with st.sidebar:
        st.header("Настройки")

        source = st.radio(
            "Откуда брать файлы",
            ["Ссылка Битрикс24", "Загрузить файлы", "Папка"]
            if SERVER_MODE
            else ["Папка", "Ссылка Битрикс24", "Загрузить файлы"],
            help=(
                "«Папка» — программа сама обходит каталог. На сервере это работает только "
                "для папок, доступных серверу. «Загрузить файлы» — выбрать книги в браузере."
            ),
        )
        folder, uploads, bitrix_url = "", [], ""

        if source == "Ссылка Битрикс24":
            bitrix_url = st.text_input(
                "Публичная ссылка на папку Диска",
                key="bitrix_url",
                placeholder="https://team.rosmetrade.ru/~XXXXX",
                help=(
                    "Ссылка «Поделиться» на папку Битрикс24 Диска. "
                    "Файлы скачиваются во временный каталог и удаляются после разбора."
                ),
            )
        elif source == "Папка":
            folder = st.text_input(
                "Путь к папке с БП",
                key="folder_input",
                placeholder=r"\\server\share\БП   или   D:\БП",
                help="Локальная, сетевая (\\\\сервер\\папка) или папка на удалённом рабочем столе.",
            )

            if dialog_available():
                if st.button("📂 Выбрать папку…", use_container_width=True):
                    chosen, error = pick_folder_dialog(folder or os.path.expanduser("~"))
                    if error:
                        st.session_state["dialog_error"] = error
                    elif chosen:
                        st.session_state.pop("dialog_error", None)
                        set_folder(os.path.normpath(chosen))

                if st.session_state.get("dialog_error"):
                    st.warning(st.session_state["dialog_error"])

            _render_browser(folder)
        else:
            uploads = st.file_uploader(
                "Файлы бизнес-планов",
                type=["xlsx", "xlsm"],
                accept_multiple_files=True,
                help="Можно перетащить сразу несколько файлов. На сервере они не сохраняются.",
            )

        recursive = st.checkbox("Просматривать вложенные папки", value=True)
        only_v0 = st.checkbox(
            "Только листы, содержащие «v0»",
            value=False,
            help="Ускоряет обработку, если блок всегда лежит на v0-листах.",
        )
        show_ok = st.checkbox("Показывать строки без расхождений", value=True)
        run = st.button("▶️ Запустить проверку", type="primary", use_container_width=True)

    if run:
        if source == "Ссылка Битрикс24":
            bitrix_url = bitrix_url.strip()
            if not bitrix_url.startswith(("http://", "https://")):
                st.error("Вставьте публичную ссылку на папку Диска, начинающуюся с https://")
                return
        elif source == "Папка":
            folder = folder.strip().strip('"')
            if not folder:
                st.error("Укажите путь к папке.")
                return
            if not os.path.isdir(folder):
                st.error(
                    f"Папка не найдена или недоступна: {folder}\n\n"
                    + (
                        "Сервер видит только свои папки и примонтированные сетевые диски. "
                        "Проще воспользоваться режимом «Загрузить файлы»."
                        if SERVER_MODE
                        else "Проверьте, что сетевой диск подключён и есть права на чтение."
                    )
                )
                return
            st.session_state["folder"] = folder
            save_last_folder(folder)
        elif not uploads:
            st.error("Выберите хотя бы один файл.")
            return

        bar = st.progress(0.0, text="Подготовка…")

        def progress(done, total, name):
            bar.progress(done / max(total, 1), text=f"[{done}/{total}] {name}")

        started = dt.datetime.now()
        with st.spinner("Обработка файлов…"):
            if source == "Ссылка Битрикс24":
                report = scan_bitrix_link(
                    bitrix_url,
                    recursive=recursive,
                    sheet_filter="v0" if only_v0 else "",
                    progress=progress,
                )
            elif source == "Папка":
                report = scan_folder(
                    folder,
                    recursive=recursive,
                    sheet_filter="v0" if only_v0 else "",
                    progress=progress,
                )
            else:
                report = scan_uploads(
                    uploads,
                    sheet_filter="v0" if only_v0 else "",
                    progress=progress,
                )
        bar.empty()

        st.session_state["report"] = report
        st.session_state["elapsed"] = (dt.datetime.now() - started).total_seconds()

    report = st.session_state.get("report")
    if report is None:
        hints = {
            "Ссылка Битрикс24": "Вставьте ссылку на папку Битрикс24 слева и нажмите «Запустить проверку».",
            "Загрузить файлы": "Загрузите файлы слева и нажмите «Запустить проверку».",
            "Папка": "Укажите папку слева и нажмите «Запустить проверку».",
        }
        st.info(hints.get(source, hints["Папка"]))
        return

    ok = sum(1 for r in report.rows if r.status == "ОК")
    bad = sum(1 for r in report.rows if r.status == "Расхождение")
    empty = sum(1 for r in report.rows if r.status == "Не заполнен")
    unknown = len(report.rows) - ok - bad - empty

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Файлов", report.files_scanned)
    c2.metric("Листов", report.sheets_scanned)
    c3.metric("✅ Совпадает", ok)
    c4.metric("❌ Расхождений", bad)
    c5.metric("⬜ Не заполнено", empty)
    c6.metric("⚠️ Не определено", unknown)
    st.caption(f"Время обработки: {st.session_state.get('elapsed', 0):.1f} с")

    if not report.rows:
        st.warning("Блок «Расчет процентов» не найден ни на одном листе.")
    else:
        df = pd.DataFrame([r.as_row() for r in report.rows]).rename(columns=COLUMNS)
        df = df[list(COLUMNS.values())]
        for col in ("Кол-во месяцев по строкам", "Кол-во месяцев по ячейке"):
            df[col] = df[col].map(_fmt_num)
        df["Чистая прибыль"] = df["Чистая прибыль"].map(_fmt_money)

        view = df if show_ok else df[df["Статус"] != "ОК"]
        st.subheader(f"Результаты ({len(view)} строк)")
        st.dataframe(
            view.style.apply(_row_style, axis=1),
            use_container_width=True,
            hide_index=True,
            height=min(700, 80 + 35 * len(view)),
        )

        stamp = dt.datetime.now().strftime("%Y-%m-%d_%H-%M")
        st.download_button(
            "⬇️ Скачать отчёт в Excel",
            data=build_workbook(report, st.session_state.get("folder", "")),
            file_name=f"Проверка_БП_{stamp}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
        )

    if report.skipped:
        with st.expander(f"Пропущенные файлы ({len(report.skipped)})"):
            st.dataframe(
                pd.DataFrame(report.skipped, columns=["Файл", "Причина"]),
                use_container_width=True,
                hide_index=True,
            )


def _render_browser(folder: str) -> None:
    """Встроенный навигатор по папкам — запасной вариант, если системный диалог недоступен."""
    with st.expander("🗂 Навигатор по папкам"):
        if "browse_dir" not in st.session_state:
            start = folder if folder and os.path.isdir(folder) else os.path.expanduser("~")
            st.session_state["browse_dir"] = os.path.abspath(start)
        current = st.session_state["browse_dir"]

        # ключ завязан на текущую папку: иначе Streamlit сохранит старое значение
        # виджета и поле не обновится при переходе по папкам
        manual = st.text_input(
            "Перейти к пути",
            value=current,
            key=f"browse_manual_{current}",
            help="Можно вставить сетевой путь вида \\\\сервер\\папка и нажать Enter.",
        )
        manual = manual.strip().strip('"')
        if manual and manual != current:
            if os.path.isdir(manual):
                st.session_state["browse_dir"] = os.path.abspath(manual)
                st.rerun()
            else:
                st.warning("Такой папки нет или нет доступа.")

        drives = windows_drives()
        if drives:
            cols = st.columns(min(len(drives), 4))
            for idx, drive in enumerate(drives):
                if cols[idx % len(cols)].button(drive, key=f"drive_{drive}"):
                    st.session_state["browse_dir"] = drive
                    st.rerun()

        parent = os.path.dirname(current.rstrip(os.sep)) or current
        if parent != current and os.path.isdir(parent):
            if st.button("⬆️ Наверх", use_container_width=True, key="browse_up"):
                st.session_state["browse_dir"] = parent
                st.rerun()

        subfolders = list_subfolders(current)
        if not subfolders:
            st.caption("Вложенных папок нет.")
        else:
            shown = subfolders[:300]
            for name in shown:
                if st.button(f"📁 {name}", key=f"dir_{name}", use_container_width=True):
                    st.session_state["browse_dir"] = os.path.join(current, name)
                    st.rerun()
            if len(subfolders) > len(shown):
                st.caption(f"Показаны первые {len(shown)} из {len(subfolders)} папок.")

        if st.button(
            "✅ Выбрать эту папку",
            type="primary",
            use_container_width=True,
            key="browse_pick",
        ):
            set_folder(current)


def _fmt_num(value):
    """2.0 -> '2', 2.5 -> '2,5', None -> '—' (без хвоста из нулей в таблице)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).replace(".", ",")


def _fmt_money(value):
    """1234567.8 -> '1 234 568'; пусто -> '—'."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    return f"{round(float(value)):,}".replace(",", " ")


def _row_style(row):
    bg, fg = COLORS.get(row["Статус"], ("", ""))
    return [f"background-color: {bg}; color: {fg}" if bg else "" for _ in row]


if __name__ == "__main__":
    main()
