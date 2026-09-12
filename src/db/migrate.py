"""启动时执行 schema.sql（幂等）+ 已有库的增量列迁移。

schema.sql 只含 CREATE TABLE IF NOT EXISTS，已存在的表不会被改动；
新增列通过 _MIGRATION_COLUMNS 幂等补齐（ALTER TABLE ADD COLUMN，
列已存在时忽略 SQLite duplicate column name 错误）。
"""

from __future__ import annotations

from importlib import resources

SCHEMA_NAME = "schema.sql"

# (表, 列定义)：历史库升级时逐条补列
_MIGRATION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("usage_events",
     "cached_tokens INTEGER"),  # 输入中命中缓存的 token（上游可选，NULL=未上报）
)


def _read_schema() -> str:
    ref = resources.files(__package__).joinpath(SCHEMA_NAME)
    return ref.read_text(encoding="utf-8")


def apply_schema(conn) -> None:
    conn.executescript(_read_schema())
    for table, column in _MIGRATION_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
        except conn.OperationalError as error:  # 列已存在
            if "duplicate column name" not in str(error).lower():
                raise
    conn.commit()
