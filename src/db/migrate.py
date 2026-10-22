"""启动时执行 schema.sql（幂等）+ 已有库的增量列/表迁移。

schema.sql 只含 CREATE TABLE IF NOT EXISTS，已存在的表不会被改动：
- 新增列 → `_MIGRATION_COLUMNS`（ALTER TABLE ADD COLUMN，列已存在时忽略）
- 删除表 → `_MIGRATION_DROPS`（DROP TABLE IF EXISTS；schema.sql 里删定义
  不会作用于老库，遗留表必须在这里显式删，否则 schema.sql 与实际库不一致）
- 删除列 → `_MIGRATION_COLUMN_DROPS`（ALTER TABLE DROP COLUMN，列不存在时忽略）

schema 版本记在 SQLite 的 `PRAGMA user_version` 里，便于运维判断
"这个库是哪一代"；旧库（user_version=0）在升级时只补列/删表不丢数据。
"""

from __future__ import annotations

from importlib import resources

SCHEMA_NAME = "schema.sql"

# 当前 schema 版本。新增列/表、删表时 +1，并在下方对应元组里补增量。
# 10：新增 runtime_settings 表（运行时配置热更新）。新增表只需进
# schema.sql（CREATE TABLE IF NOT EXISTS 对老库同样生效），无需迁移动作。
# 11：credentials 新增 token_expires_at / token_issued_at（token 到期展示与预警）。
# 12：新增 credit_events 表（积分变动流水）。新增表只需进
# schema.sql（CREATE TABLE IF NOT EXISTS 对老库同样生效），无需迁移动作。
# 13：api_keys 新增 provider_binding / allowed_ips（多 Key 出口：渠道绑定与
# 来源 IP 白名单）；两者默认 ''，老库补列后行为不变。
# 14：新增 users / audit_events 表（账号管理体系）。新增表只需进
# schema.sql（CREATE TABLE IF NOT EXISTS 对老库同样生效），无需迁移动作。
# 15：usage_events 新增 credit_estimated（credit 是否本服务推算）；usage_hourly
# 新增 credit_estimated_known（其中推算值条数），老库补 0=全部非推算。
# 16：api_keys 新增 allowed_models / expires_at（Key 细粒度策略：模型白名单与
# 到期时间）；默认 '' / NULL，老库补列后行为不变（不限制、不过期）。
# 17：新增 alert_events 表（运维告警：四类规则命中即落一行，供管理台「站内告警
# 记录」回看）。新增表只需进 schema.sql（CREATE TABLE IF NOT EXISTS
# 对老库同样生效），无需迁移动作。
# 18：usage_events 新增 cost_usd / cost_cny，usage_hourly 新增 cost_usd_sum /
# cost_cny_sum / cost_known（成本估算：OpenRouter 刊例价 × 当时汇率，写入时定值）。
# 老库补列后历史行成本为 NULL（显示 —），不随汇率/价表变化重算。
# 19：usage_events 新增 request_id（请求 ID：响应头 X-Request-ID 同值，用于把
# 用户报的请求与明细/上游日志对齐）。老库补列后历史行为 NULL。
# 20：新增 credential_limits 表（凭证级限额护栏：保留积分 / 每日 token /
# 每日 credit / 按模型 token）。新增表只需进 schema.sql（CREATE TABLE IF NOT
# EXISTS 对老库同样生效），无需迁移动作；缺行 = 不受限，老库行为不变。
# 21：alert_events 新增 resolved_at（告警迟滞状态机确认恢复后写入；老库补列
# 后历史行为 NULL，不影响既有记录展示与静默去重）。
# 22：credentials 新增 tags（逗号分隔的运维标签，仅用于列表展示与筛选，
# 不参与调度/鉴权）。默认 ''，老库补列后全部凭证「无标签」，行为不变。
# 23：credentials 新增 quota_credits_remaining / quota_credits_total（CodeArts
# 第二本账：积分＝内置模型计费单位，与每日 token 池并行）。老库补列后为 NULL
# ＝「无积分账」，展示层隐藏该行，health / 选号行为不变。第 29 版已删除
# （福利链路下线，积分账成为唯一额度来源）。
# 24：credit_events 新增 ledger（区分两本账：pool＝每日 token 池 / credits＝
# 积分账）。老库补列后默认 'pool'——历史流水全部由主账（每日池）的 diff 产生，
# 归入 pool 即为事实，不需要回填；积分账流水从补列后的下一轮探测开始记。
# 25：credentials 新增 quota_credits_expiry_ladder（积分账到期阶梯，仅 CodeArts，
# 供管理台按账本展示到期积分：福利组＝每日池 / 内置组＝积分账）。老库补列后为
# NULL ＝「无积分到期信息」，展示层按「无到期积分」处理，health / 选号不受影响
# （选号读的是两本账合并的 quota_expiry_ladder，不读这一列）。第 29 版已删除。
# 26：usage_events 新增 api_key_id（发起请求的 API Key id，供按 Key 计量与
# 自助用量）。老库补列后历史行为 NULL ＝「未归属到具体 Key」，读路径按未归属
# 处理，与补列前行为一致；索引 idx_usage_key 在补列之后建（见 _MIGRATION_INDEXES）。
# 27：api_keys 新增 quota（Key 级配额 JSON 文本：当日请求/当日 token/当日成本/
# RPM/TPM/并发，''=不限制）。老库补列后为 '' ＝「所有 Key 不受配额约束」，
# 出口行为与补列前完全一致。
# 28：新增 model_probes 表（主动探活结论 + 人工接管标记）。新增表只需进
# schema.sql（CREATE TABLE IF NOT EXISTS 对老库同样生效），无需迁移动作；
# 老库升级后表为空 ＝「从未探活」，管理台显示「—」，行为与补表前一致。
# 29：移除福利链路（每日千万 token 免费池已下线）：credentials 删三列积分账
# （`quota_credits_remaining` / `quota_credits_total` / `quota_credits_expiry_ladder`）。
# 积分账成为 CodeArts 唯一的额度来源，直接落在通用列（`quota_remaining` 等）上，
# 故这三列不再有读者；见 `_MIGRATION_COLUMN_DROPS`。
SCHEMA_VERSION = 29

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
    # access token 签发/到期时间：上游显式 expires_at 缺失时回落
    # JWT 的 iat/exp；NULL=老库未回填（列表读到按需派生），0=确实未知
    ("credentials", "token_expires_at INTEGER"),
    ("credentials", "token_issued_at INTEGER"),
    # 多 Key 出口：空串 = 不限制，老库补列后与补列前行为一致
    ("api_keys", "provider_binding TEXT NOT NULL DEFAULT ''"),
    ("api_keys", "allowed_ips TEXT NOT NULL DEFAULT ''"),
    # 积分推算（TRAE 上游不给单请求积分）：老库补 0=历史行全按非推算处理
    ("usage_events", "credit_estimated INTEGER NOT NULL DEFAULT 0"),
    ("usage_hourly", "credit_estimated_known INTEGER NOT NULL DEFAULT 0"),
    # Key 细粒度策略：模型白名单（''=不限制）/ 到期时间（NULL=不过期），
    # 老库补列后与补列前行为一致
    ("api_keys", "allowed_models TEXT NOT NULL DEFAULT ''"),
    ("api_keys", "expires_at INTEGER"),
    # 成本估算（OpenRouter 刊例价 × 当时汇率）：老库补列后历史成本为 NULL。
    # usage_hourly 的 cost_known=0 表示该小时没有可定价明细，聚合值不可信。
    ("usage_events", "cost_usd REAL"),
    ("usage_events", "cost_cny REAL"),
    ("usage_hourly", "cost_usd_sum REAL"),
    ("usage_hourly", "cost_cny_sum REAL"),
    ("usage_hourly", "cost_known INTEGER NOT NULL DEFAULT 0"),
    # 请求 ID（响应头 X-Request-ID 同值）：老库补列后历史行为 NULL。
    ("usage_events", "request_id TEXT"),
    # 告警恢复时刻（迟滞状态机确认恢复后写入）：老库补列后历史行为 NULL
    # （全部视为「仍在告警中」/未恢复，不影响既有记录展示）。
    ("alert_events", "resolved_at INTEGER"),
    # 运维标签（仅列表展示与筛选，不参与调度）：老库补 '' 后全部凭证无标签，
    # 筛选界面默认「全部标签」下行为与补列前一致。
    ("credentials", "tags TEXT NOT NULL DEFAULT ''"),
    # 变动流水按账本分流：老库补 'pool'——历史行全部由主账（每日池）diff 产生，
    # 归入 pool 即为事实；CodeArts 今后写 'credits'（见 repo.quota_ledger）。
    ("credit_events", "ledger TEXT NOT NULL DEFAULT 'pool'"),
    # 发起请求的 API Key（按 Key 计量 / 自助用量）：老库补列后历史行为 NULL
    # ＝「未归属到具体 Key」，读路径按未归属处理，与补列前一致。
    ("usage_events", "api_key_id TEXT"),
    # Key 级配额（JSON 文本）：老库补 '' ＝「不限制」，出口行为不变。
    ("api_keys", "quota TEXT NOT NULL DEFAULT ''"),
)

# 已废弃的表：schema.sql 里已删定义，但老库里可能还留着，必须显式清理。
# 签到去重改为当日内存态、模型列表改为进程内 TTL 缓存后这两张表零引用。
_MIGRATION_DROPS: tuple[str, ...] = ("checkins", "model_cache")

# (表, 列)：已废弃的列，schema.sql 里已删定义，但老库里可能还留着。
# `ALTER TABLE ADD COLUMN` 只加不删，故必须显式 DROP；列不存在时忽略
# （新库本就没有这些列）。删列要求 SQLite ≥ 3.35（2021，本机 3.54）。
_MIGRATION_COLUMN_DROPS: tuple[tuple[str, str], ...] = (
    # 福利链路下线：积分账成为 CodeArts 唯一的额度来源，直接落在通用列上。
    ("credentials", "quota_credits_remaining"),
    ("credentials", "quota_credits_total"),
    ("credentials", "quota_credits_expiry_ladder"),
)

# (索引名, 表, 列)：**建在补列之后**的索引。
# 为什么不能只放 schema.sql：`CREATE INDEX` 在 `executescript` 阶段执行，而老库
# 此时还没补上新列，会直接 `no such column` 报错（见 idx_alert_unresolved 的
# resolved_at——它本身是第 21 版迁移补的列）。放这里 = 先补列再建索引。
_MIGRATION_INDEXES: tuple[tuple[str, str, str], ...] = (
    # 侧栏角标只数「仍在告警中」（resolved_at IS NULL）：避免每次轮询全表扫。
    ("idx_alert_unresolved", "alert_events", "resolved_at"),
    # 按 Key 计量 / 自助用量只查「某把 Key + 时间窗」，必须走索引不能全表扫；
    # api_key_id 是第 26 版才补的列，索引只能在这里建。
    ("idx_usage_key", "usage_events", "api_key_id, ts"),
)


def _read_schema() -> str:
    ref = resources.files(__package__).joinpath(SCHEMA_NAME)
    return ref.read_text(encoding="utf-8")


def schema_version(conn) -> int:
    """读取当前库的 schema 版本（新库为 0）。"""
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def apply_schema(conn) -> None:
    # 版本回退护栏：本程序只认「本代或更老」的库。老二进制打开新库时若照常执行，
    # 末尾的 `PRAGMA user_version = SCHEMA_VERSION` 会把版本号**写回**旧值——运维
    # 据此判断「库是哪一代」即被误导，且老代码可能不认识新列而写坏数据。宁可启动
    # 失败并让人显式处理（升级程序或换回对应版本的库）。
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"数据库 schema 版本 {current} 高于本程序支持的 {SCHEMA_VERSION}；"
            "请升级程序，或使用与该版本匹配的数据库备份")
    conn.executescript(_read_schema())
    for table, column in _MIGRATION_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
        except conn.OperationalError as error:  # 列已存在
            if "duplicate column name" not in str(error).lower():
                raise
    for table in _MIGRATION_DROPS:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    for table, column in _MIGRATION_COLUMN_DROPS:
        try:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        except conn.OperationalError as error:  # 列不存在（新库 / 已删过）
            if "no such column" not in str(error).lower():
                raise
    for name, table, column in _MIGRATION_INDEXES:
        conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({column})")
    # PRAGMA 不支持参数绑定，版本号来自本模块常量（非外部输入）
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
