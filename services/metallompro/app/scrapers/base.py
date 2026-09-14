"""Общие утилиты скраперов (перенос из v1 utils.py)."""
from __future__ import annotations
import re
import httpx

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}


def make_client(timeout: int = 25) -> httpx.Client:
    return httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True)


def parse_price(text: str) -> float | None:
    if not text:
        return None
    s = text.strip().replace(" ", " ").replace(" ", "")
    s = re.sub(r"[^\d.,]", "", s)
    if not s:
        return None
    dots, commas = s.count("."), s.count(",")
    if dots == 0 and commas == 1:
        after = s.split(",")[1]
        s = s.replace(",", "") if len(after) == 3 else s.replace(",", ".")
    elif commas == 0 and dots == 1:
        if len(s.split(".")[1]) == 3:
            s = s.replace(".", "")
    elif dots == 1 and commas == 1:
        s = (s.replace(".", "").replace(",", ".") if s.index(".") < s.index(",")
             else s.replace(",", ""))
    elif dots >= 2:
        s = s.replace(".", "")
    elif commas >= 2:
        s = s.replace(",", "")
    try:
        v = float(s)
        return v if v > 0 else None
    except ValueError:
        return None


_METAL_KEYWORDS = [
    ("шред", "SHRED"), ("чугун", "CAST"), ("12а", "A12_SCRAP"), ("5а", "A5_SCRAP"),
    ("3а", "A3_SCRAP"), ("3б", "A3_SCRAP"), ("чермет", "A3_SCRAP"), ("черн", "A3_SCRAP"),
    ("медн", "COPPER"), ("медь", "COPPER"), ("алюмини", "ALUM"),
    ("свинец", "LEAD"), ("свинц", "LEAD"), ("цинк", "ZINC"), ("никел", "NICKEL"),
    ("лом стал", "A3_SCRAP"), ("металлол", "A3_SCRAP"),
]


def detect_metal(text: str) -> str | None:
    t = text.lower()
    for kw, code in _METAL_KEYWORDS:
        if kw in t:
            return code
    return None
