"""Подключение к БД: SQLite для разработки, PostgreSQL в проде (DATABASE_URL)."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .models import Base

log = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get(
    "METOPTTORG_DATA_DIR",
    Path(__file__).resolve().parents[2] / "data"))
DEFAULT_URL = f"sqlite:///{DATA_DIR / 'metopttorg.db'}"

DATABASE_URL = os.environ.get("DATABASE_URL", DEFAULT_URL)

engine = create_engine(
    DATABASE_URL,
    future=True,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    ensure_schema()


def ensure_schema() -> list[str]:
    """Добавляет колонки, появившиеся в моделях после создания базы.

    Без этого каждое расширение схемы ломало бы работающий сервер: create_all
    создаёт только отсутствующие ТАБЛИЦЫ, но не колонки. Полноценные миграции
    (alembic) избыточны, пока изменения сводятся к добавлению колонок.
    """
    from sqlalchemy import inspect, text

    added: list[str] = []
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                col_type = column.type.compile(engine.dialect)
                conn.execute(text(
                    f'ALTER TABLE "{table.name}" ADD COLUMN '
                    f'"{column.name}" {col_type}'))
                added.append(f"{table.name}.{column.name}")
    if added:
        log.info("схема дополнена колонками: %s", ", ".join(added))
    return added


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
