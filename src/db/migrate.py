"""启动时执行 schema.sql（幂等）+ 已有库的增量列/表迁移。

schema.sql 只含 CREATE TABLE IF NOT EXISTS，已存在的表不会被改动：
- 新增列 → `_MIGRATION_COLUMNS`（ALTER TABLE ADD COLUMN，列已存在时忽略）
- 删除表 → `_MIGRATION_DROPS`（DROP TABLE IF EXISTS；schema.sql 里删定义
  不会作用于老库，遗留表必须在这里显式删，否则 schema.sql 与实际库不一致）

schema 版本记在 SQLite 的 `PRAGMA user_version` 里，便于运维判断
"这个库是哪一代"；旧库（user_version=0）在升级时只补列/删表不丢数据。
"""

from __future__ import annotations

from importlib import resources

SCHEMA_NAME = "schema.sql"

# 当前 schema 版本。新增列/表、删表时 +1，并在下方对应元组里补增量。
# 10：新增 runtime_settings 表（B3.2 运行时配置热更新）。新增表只需进
# schema.sql（CREATE TABLE IF NOT EXISTS 对老库同样生效），无需迁移动作。
# 11：credentials 新增 token_expires_at / token_issued_at（B3.3
# token 到期展示与预警）。
SCHEMA_VERSION = 11

# (表, 列定义)：历史库升级时逐条补列
_MIGRATION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("usage_events",
     "cached_tokens INTEGER"),  # 输入中命中缓存的 token（上游可选，NULL=未上报）
    ("usage_hourly",
     "ttfb_sum INTEGER NOT NULL DEFAULT 0"),  # 首字延迟聚合（图表维度，老库补 0）
    ("credentials",
     "quota_expiry_ladder TEXT"),  # 到期阶梯 JSON：选号按窗口内到期积分排序
    # 总览改为读小时汇总后，这两项也必须能在小时表里聚合（老库历史行补 0，
    # 历史小时的数值无法回填：明细已不在，只能接受旧时段显示 0）
    ("usage_hourly", "reasoning_tokens INTEGER NOT NULL DEFAULT 0"),
    ("usage_hourly", "cached_tokens INTEGER NOT NULL DEFAULT 0"),
    ("usage_hourly", "cached_known INTEGER NOT NULL DEFAULT 0"),
    # 成长中心最近一次运行结果（列表页直接显示，不必再查 events 表）
    ("credentials", "growth_last_run_at INTEGER"),
    ("credentials", "growth_last_result TEXT"),
    # 额度包明细（展示用，含包名）：与选号指标 quota_expiry_ladder 分开，
    # 避免为管理台展示改动调度排序行为
    ("credentials", "quota_packages TEXT"),
    # access token 签发/到期时间（B3.3）：上游显式 expires_at 缺失时回落
    # JWT 的 iat/exp；NULL=老库未回填（列表读到按需派生），0=确实未知
    ("credentials", "token_expires_at INTEGER"),
    ("credentials", "token_issued_at INTEGER"),
)

# 已废弃的表：schema.sql 里已删定义，但老库里可能还留着，必须显式清理。
# 签到去重改为当日内存态、模型列表改为进程内 TTL 缓存后这两张表零引用。
_MIGRATION_DROPS: tuple[str, ...] = ("checkins", "model_cache")


def _read_schema() -> str:
    ref = resources.files(__package__).joinpath(SCHEMA_NAME)
    return ref.read_text(encoding="utf-8")


def schema_version(conn) -> int:
    """读取当前库的 schema 版本（新库为 0）。"""
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def apply_schema(conn) -> None:
    conn.executescript(_read_schema())
    for table, column in _MIGRATION_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
        except conn.OperationalError as error:  # 列已存在
            if "duplicate column name" not in str(error).lower():
                raise
    for table in _MIGRATION_DROPS:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    # PRAGMA 不支持参数绑定，版本号来自本模块常量（非外部输入）
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
