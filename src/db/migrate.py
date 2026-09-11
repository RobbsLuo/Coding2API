"""启动时执行 schema.sql（幂等）。"""

from __future__ import annotations

from importlib import resources

SCHEMA_NAME = "schema.sql"


def _read_schema() -> str:
    ref = resources.files(__package__).joinpath(SCHEMA_NAME)
    return ref.read_text(encoding="utf-8")


def apply_schema(conn) -> None:
    conn.executescript(_read_schema())
    conn.commit()
