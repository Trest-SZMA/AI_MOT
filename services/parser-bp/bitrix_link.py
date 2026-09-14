# -*- coding: utf-8 -*-
"""
Чтение папки Битрикс24 по публичной ссылке.

Публичная ссылка вида https://team.rosmetrade.ru/~PZxJj открывает страницу
Диска со списком файлов и подпапок. Скачивание работает только внутри той же
сессии (в ссылке лежит одноразовый token, привязанный к PHPSESSID), поэтому
обход и загрузка идут через один requests.Session.

Учётные данные не нужны: ссылка открыта на чтение всем, у кого она есть.
"""

from __future__ import annotations

import html
import os
import re
import shutil
import tempfile
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import requests

from parser_core import SUPPORTED_EXT, LEGACY_EXT, ScanReport, scan_one_file

USER_AGENT = "Mozilla/5.0 (compatible; ParserBP/1.0)"
TIMEOUT = 60
MAX_FOLDERS = 3000         # предохранитель от зацикливания; страницы списка тоже считаются
MAX_FILES = 20000
DOWNLOAD_WORKERS = 8       # скачивание идёт параллельно, разбор — последовательно
DOWNLOAD_BATCH = 24        # пачка: держим в памяти не больше 24 книг за раз

_RE_DOWNLOAD = re.compile(r'href="([^"]*downloadFileUnderFolder[^"]*)"')
_RE_SUBFOLDER = re.compile(r'href="([^"]*/default/\?[^"]*path=[^"&]+[^"]*)"')
# Список файлов в папке выдаётся по 25 штук, остальные — по ссылкам «Страницы: 1 2 3…»
# ⚠️ ДВЕ РАЗМЕТКИ. 13.09.2026 Битрикс сменил вид публичной папки: вместо
# «bx-disk-nav-page» со списком страниц стала «disk-ext-folder-list__page --nav»
# с одной ссылкой «Следующая» (rel="next", ?pageNumber=N), а class и href
# теперь на разных строках. Старая регулярка молча находила ноль страниц, обход
# видел по 25 файлов на папку — 215 файлов вместо ~1800, и предохранитель
# еженедельной выгрузки (261 строка против 5771) правильно её остановил.
# Ловим обе разметки; «Следующая» ведёт по цепочке, страница за страницей.
_RE_NAV_PAGE = re.compile(
    r'class="(?:bx-disk-nav-page|disk-ext-folder-list__page[^"]*)"[^>]*?href="([^"]+)"',
    re.S)
_RE_TOTAL = re.compile(r'(?:bx-disk-total-grid-item|disk-ext-folder-list__total-value)">(\d+)<')
_RE_FILENAME_UTF8 = re.compile(r"filename\*=utf-8''([^;]+)", re.I)
_RE_FILENAME = re.compile(r'filename="([^"]+)"', re.I)
_RE_FILE_ID = re.compile(r"fileId=(\d+)")


@dataclass
class RemoteFile:
    name: str
    url: str
    folder: str
    file_id: str = ""


def is_bitrix_link(url: str) -> bool:
    url = (url or "").strip()
    return url.startswith("http://") or url.startswith("https://")


def _origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _absolute(base: str, href: str) -> str:
    return urllib.parse.urljoin(base, html.unescape(href))


def _folder_title(url: str) -> str:
    """Из ?path=%2FТендер... достаёт человекочитаемое имя подпапки."""
    query = urllib.parse.urlsplit(url).query
    path = urllib.parse.parse_qs(query).get("path", [""])[0]
    return path.strip("/") or "корень"


def _open_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    return session


def _page_files(text: str, origin: str, title: str) -> list[RemoteFile]:
    """Файлы страницы, по одному на fileId: одна книга даёт несколько ссылок в разметке."""
    out: list[RemoteFile] = []
    seen_ids: set[str] = set()
    for href in dict.fromkeys(_RE_DOWNLOAD.findall(text)):
        url = _absolute(origin, href)
        m = _RE_FILE_ID.search(url)
        file_id = m.group(1) if m else url
        if file_id in seen_ids:
            continue
        seen_ids.add(file_id)
        out.append(RemoteFile(name="", url=url, folder=title, file_id=file_id))
    return out


def _page_subfolders(text: str, origin: str) -> list[str]:
    return [_absolute(origin, href) for href in dict.fromkeys(_RE_SUBFOLDER.findall(text))]


def _page_next_pages(text: str, origin: str) -> list[str]:
    """Ссылки «Страницы: 2 3 4…» — без них видны только первые 25 файлов папки."""
    return [_absolute(origin, href) for href in dict.fromkeys(_RE_NAV_PAGE.findall(text))]


def _fetch(session: requests.Session, item: "RemoteFile"):
    """Скачивает один файл. Возвращает (файл, ответ, текст ошибки)."""
    try:
        resp = session.get(item.url, timeout=TIMEOUT)
        resp.raise_for_status()
        return item, resp, ""
    except requests.RequestException as exc:
        return item, None, str(exc)


def _real_name(response: requests.Response, fallback: str) -> str:
    """Настоящее имя файла берём из Content-Disposition (filename*=utf-8)."""
    disposition = response.headers.get("Content-Disposition", "")
    m = _RE_FILENAME_UTF8.search(disposition)
    if m:
        return urllib.parse.unquote(m.group(1))
    m = _RE_FILENAME.search(disposition)
    if m:
        return m.group(1)
    return fallback


def scan_bitrix_link(
    url: str,
    recursive: bool = True,
    sheet_filter: str = "",
    progress=None,
    total_hint: int = 0,
) -> ScanReport:
    """
    Скачивает книги из публичной папки Битрикс24 и разбирает их.

    Файлы кладутся во временный каталог и удаляются сразу после разбора.
    """
    report = ScanReport()
    needle = sheet_filter.strip().lower()
    session = _open_session()
    tmp_dir = tempfile.mkdtemp(prefix="bp_bitrix_")
    done = 0
    total = total_hint or 0

    try:
        first = session.get(url, timeout=TIMEOUT)
        first.raise_for_status()
        origin = _origin(first.url)
        queue = [first.url]
        seen = {urllib.parse.urlsplit(first.url).query}
        pages = {first.url: first.text}
        seen_files: set[str] = set()      # один и тот же файл не качаем дважды

        while queue:
            page_url = queue.pop(0)
            folder = _folder_title(page_url)

            text = pages.pop(page_url, None)
            if text is None:
                try:
                    resp = session.get(page_url, timeout=TIMEOUT)
                    resp.raise_for_status()
                    text = resp.text
                except requests.RequestException as exc:
                    report.skipped.append((folder, f"папка не открылась: {exc}"))
                    continue

            files = [f for f in _page_files(text, origin, folder) if f.file_id not in seen_files]
            seen_files.update(f.file_id for f in files)

            # Ссылки на другие страницы и подпапки забираем сразу, но НЕ открываем
            # до того, как скачаем файлы этой страницы: token живёт до следующей
            # загрузки любой страницы.
            follow: list[str] = list(_page_next_pages(text, origin))
            if recursive:
                follow += _page_subfolders(text, origin)
            for next_url in follow:
                key = urllib.parse.urlsplit(next_url).query
                if key not in seen and len(seen) <= MAX_FOLDERS:
                    seen.add(key)
                    queue.append(next_url)

            if done >= MAX_FILES:
                report.skipped.append((folder, f"файлов больше {MAX_FILES} — остальные не обработаны"))
                break
            if not files:
                continue
            if not total:
                total = len(files)          # хотя бы что-то показать в индикаторе

            # Скачиваем пачками в несколько потоков (узкое место — сеть), а разбираем
            # последовательно: openpyxl упирается в процессор, потоки там не помогут.
            for start in range(0, len(files), DOWNLOAD_BATCH):
                batch = files[start : start + DOWNLOAD_BATCH]
                with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
                    fetched = list(pool.map(lambda it: _fetch(session, it), batch))

                for item, resp, error in fetched:
                    done += 1
                    if progress:
                        progress(done, max(total, done), f"{folder}")
                    if error:
                        report.skipped.append((f"{folder}", f"не скачался: {error}"))
                        continue

                    name = _real_name(resp, "")
                    if not name:
                        report.skipped.append(
                            (folder, "сервер не сообщил имя файла — вероятно, ссылка устарела")
                        )
                        continue

                    lowered = name.lower()
                    if not lowered.endswith(SUPPORTED_EXT + LEGACY_EXT):
                        continue   # в папке лежат не только книги — прочее пропускаем

                    if not resp.content.startswith(b"PK") and lowered.endswith(SUPPORTED_EXT):
                        report.skipped.append((name, "вместо книги пришла страница — ссылка устарела"))
                        continue

                    path = os.path.join(tmp_dir, f"{done}_{os.path.basename(name)}")
                    try:
                        with open(path, "wb") as fh:
                            fh.write(resp.content)
                    except OSError as exc:
                        report.skipped.append((name, f"не удалось сохранить: {exc}"))
                        continue

                    shown = f"{folder}/{name}" if folder != "корень" else name
                    scan_one_file(path, name, needle, report, report_path=shown)
                    try:
                        os.remove(path)
                    except OSError:
                        pass
    except requests.RequestException as exc:
        report.skipped.append((url, f"не удалось открыть ссылку: {exc}"))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if not report.files_scanned and not report.skipped:
        report.skipped.append((url, "в папке не найдено ни одной книги Excel"))

    return report
