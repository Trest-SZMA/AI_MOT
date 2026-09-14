"""Конфигурация MetalLomPro 2.0 — всё из переменных окружения (.env)."""
from __future__ import annotations
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent.parent          # v2/
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# БД: Postgres в проде, SQLite как dev-фолбэк
DATABASE_URL = os.getenv("DATABASE_URL") or f"sqlite:///{DATA_DIR / 'metallompro.db'}"

# ИИ
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "perplexity").lower()   # perplexity | anthropic | off
PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

METALS_API_KEY = os.getenv("METALS_API_KEY", "")

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")

# Пользователи «логин:пароль,логин:пароль» — равные права
# Задаётся ТОЛЬКО через окружение (/etc/metallompro.env или env/metallompro.env):
# без переменной ни один пользователь не сможет войти.
USERS_RAW = os.getenv("USERS", "")

INBOX_1C = Path(os.getenv("INBOX_1C", DATA_DIR / "inbox_1c"))
INBOX_1C.mkdir(parents=True, exist_ok=True)
ARCHIVE_1C = DATA_DIR / "archive"
ARCHIVE_1C.mkdir(parents=True, exist_ok=True)

# SQL-база экстрактора 1С (подключается позже)
EXTRACTOR_DB_URL = os.getenv("EXTRACTOR_DB_URL", "")

TZ = os.getenv("TZ", "Asia/Yekaterinburg")

# Интервалы фоновых задач (часы)
SCRAPE_INTERVAL_H = float(os.getenv("SCRAPE_INTERVAL_H", "6"))
BRIEFING_HOUR = int(os.getenv("BRIEFING_HOUR", "7"))   # локальное время генерации утреннего брифинга
