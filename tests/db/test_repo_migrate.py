"""凭证仓储：schema 迁移声明与老库升级（列/表补齐）。"""

from __future__ import annotations

import json
import sqlite3

from src.db.conn import Database
from src.db.migrate import apply_schema


def test_migration_declarations_match_schema_sql():
    """防漂移：迁移声明的列必须在 schema.sql 里存在，删表清单不得重建。

    `_MIGRATION_COLUMNS` 里的列名写错、或 schema.sql 删掉了对应的新库列定义，
    都会让两边静默分叉（新库/老库结构不一致），这里在测试期就抦住。
    """
    from src.db.migrate import (
        _MIGRATION_COLUMN_DROPS,
        _MIGRATION_COLUMNS,
        _MIGRATION_DROPS,
        _read_schema,
    )

    schema = _read_schema()
    for table, column_def in _MIGRATION_COLUMNS:
        column = column_def.split()[0]
        assert f"CREATE TABLE IF NOT EXISTS {table}" in schema, table
        # 取该表定义段落，断言列在其中
        segment = schema.split(f"CREATE TABLE IF NOT EXISTS {table}", 1)[1]
        segment = segment.split(");", 1)[0]
        assert column in segment, f"{table}.{column} 不在 schema.sql"
    for table in _MIGRATION_DROPS:
        # 删掉的表不得在 schema.sql 里重建，否则每次启动都会建了又删
        assert f"CREATE TABLE IF NOT EXISTS {table}" not in schema, table

    for table, column in _MIGRATION_COLUMN_DROPS:
        # 已删列不得再出现在 schema.sql，否则新库每次建表都带着它、
        # 紧接着又被删列步骤删掉（每次启动都在建了又删）
        assert f"CREATE TABLE IF NOT EXISTS {table}" in schema, table
        segment = schema.split(f"CREATE TABLE IF NOT EXISTS {table}", 1)[1]
        segment = segment.split(");", 1)[0]
        assert column not in segment, f"{table}.{column} 不该出现在 schema.sql"
        # 补列清单里也不得残留同名列（补了又删）
        assert all(col_def.split()[0] != column for t, col_def in _MIGRATION_COLUMNS), column


def test_migration_indexes_not_in_schema_sql():
    """补列后建的索引不得写进 schema.sql。

    写进去会让老库在 `executescript` 阶段就撞上「索引引用的列还没补」而启动
    失败——这条测试就是防有人图省事把它挪回 schema.sql。
    """
    from src.db.migrate import _MIGRATION_INDEXES, _read_schema

    schema = _read_schema()
    for name, table, column in _MIGRATION_INDEXES:
        assert f"CREATE INDEX IF NOT EXISTS {name}" not in schema, name
        assert f"CREATE TABLE IF NOT EXISTS {table}" in schema, table
        assert column            # 列名非空（真正由迁移循环补）

def test_migrate_adds_cached_tokens_to_legacy_db(tmp_path):
    """老库升级：usage_events 无 cached_tokens 列时幂等补齐，且可写入。"""
    import sqlite3

    from src.db.migrate import SCHEMA_VERSION, apply_schema

    db = Database(tmp_path / "legacy.sqlite3")
    conn = sqlite3.connect(db.path)
    conn.execute("""
        CREATE TABLE usage_events (
            id TEXT PRIMARY KEY, ts INTEGER NOT NULL, username TEXT NOT NULL,
            provider TEXT NOT NULL, credential_id TEXT, model TEXT NOT NULL,
            ok INTEGER NOT NULL, error_type TEXT, input_tokens INTEGER,
            output_tokens INTEGER, reasoning_tokens INTEGER, credit REAL,
            latency_ms INTEGER, ttfb_ms INTEGER)
    """)
    # 旧版 usage_hourly：无 ttfb_sum 列（图表指标升级前的结构）
    conn.execute("""
        CREATE TABLE usage_hourly (
            hour_utc INTEGER NOT NULL, username TEXT NOT NULL,
            provider TEXT NOT NULL, model TEXT NOT NULL,
            requests INTEGER NOT NULL DEFAULT 0, ok_count INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
            credit_sum REAL, credit_known INTEGER NOT NULL DEFAULT 0,
            latency_sum INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (hour_utc, username, provider, model))
    """)
    # 该表内已有一行历史汇总：补列后必须保留，新列取默认 0
    conn.execute("INSERT INTO usage_hourly (hour_utc, username, provider, model, requests) "
                 "VALUES (1, 'u', 'trae', 'm', 5)")
    # 旧版 credentials：无 quota_expiry_ladder 列（到期排序指标升级前的结构）
    conn.execute("""
        CREATE TABLE credentials (
            id TEXT PRIMARY KEY, provider TEXT NOT NULL, data_enc TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1, disabled INTEGER NOT NULL DEFAULT 0,
            disabled_reason TEXT, pinned INTEGER NOT NULL DEFAULT 0,
            health INTEGER, cooling_until INTEGER, err_count INTEGER NOT NULL DEFAULT 0,
            quota_remaining REAL, quota_total REAL, quota_cycle_end INTEGER,
            quota_probed_at INTEGER, created_at INTEGER NOT NULL, added_by TEXT)
    """)
    conn.commit()
    # 老库遗留的废弃表（schema.sql 已删定义，但老库里还在）
    conn.execute("CREATE TABLE checkins (provider TEXT, account_key TEXT)")
    conn.execute("CREATE TABLE model_cache (provider TEXT, model_id TEXT)")
    # 旧版 alert_events：无 resolved_at 列（告警迟滞/恢复升级前的结构）
    conn.execute("""
        CREATE TABLE alert_events (
            id TEXT PRIMARY KEY, ts INTEGER NOT NULL, rule TEXT NOT NULL,
            severity TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '', message TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '', delivered INTEGER NOT NULL DEFAULT 0,
            delivery_error TEXT)
    """)
    conn.execute("INSERT INTO alert_events (id, ts, rule, severity, scope, message) "
                 "VALUES ('alert_old', 5, 'pool_empty', 'critical', 'pool', '旧记录')")
    conn.commit()
    conn.close()

    apply_schema(db.connect())
    events_columns = {row[1] for row in db.connect().execute("PRAGMA table_info(usage_events)")}
    assert "cached_tokens" in events_columns
    # 积分推算标记（TRAE 无上游积分）：老库补 0=历史行全按非推算处理
    assert "credit_estimated" in events_columns
    # 成本估算两列（OpenRouter 刊例价 × 当时汇率）：老库补列后历史行为 NULL
    assert {"cost_usd", "cost_cny"} <= events_columns
    # 请求 ID（响应头 X-Request-ID 同值）：老库补列后历史行为 NULL
    assert "request_id" in events_columns
    # 发起请求的 API Key：老库补列后历史行 NULL＝未归属，读路径按未归属处理
    assert "api_key_id" in events_columns
    legacy_event = db.connect().execute(
        "SELECT api_key_id FROM usage_events LIMIT 1").fetchone()
    assert legacy_event is None                  # 老库无明细行，仅确认列可读
    # 按 Key 计量的索引建在补列之后（api_key_id 是第 26 版才补的列）
    event_indexes = {row[1] for row in db.connect().execute(
        "PRAGMA index_list(usage_events)")}
    assert "idx_usage_key" in event_indexes
    # 失败归因的「换号救回」按 request_id 关联（request_id 是第 19 版补的列）
    assert "idx_usage_request" in event_indexes
    alert_columns = {row[1] for row in db.connect().execute("PRAGMA table_info(alert_events)")}
    # 告警恢复时刻：老库补列后历史行 NULL（视为未恢复，不影响既有记录展示）
    assert "resolved_at" in alert_columns
    legacy_alert = db.connect().execute(
        "SELECT message, resolved_at FROM alert_events WHERE id = 'alert_old'").fetchone()
    assert tuple(legacy_alert) == ("旧记录", None)   # 历史记录保留，新列取默认
    # 补列后建的索引（resolved_at 是第 21 版才补的列，索引必须建在补列之后）
    indexes = {row[1] for row in db.connect().execute("PRAGMA index_list(alert_events)")}
    assert "idx_alert_unresolved" in indexes
    hourly_columns = {row[1] for row in db.connect().execute("PRAGMA table_info(usage_hourly)")}
    assert "ttfb_sum" in hourly_columns
    assert "credit_estimated_known" in hourly_columns
    # 成本聚合三列：老库补列后历史小时 cost_known=0（聚合值不可信）
    assert {"cost_usd_sum", "cost_cny_sum", "cost_known"} <= hourly_columns
    # 总览改读小时汇总后新增的三列（老库历史行补 0，数值无法回填）
    assert {"reasoning_tokens", "cached_tokens", "cached_known"} <= hourly_columns
    legacy_hourly = db.connect().execute(
        "SELECT requests, reasoning_tokens, cached_known FROM usage_hourly "
        "WHERE hour_utc = 1").fetchone()
    assert tuple(legacy_hourly) == (5, 0, 0)      # 历史汇总保留，新列取默认
    legacy_est = db.connect().execute(
        "SELECT credit_estimated_known FROM usage_hourly WHERE hour_utc = 1").fetchone()
    assert legacy_est[0] == 0                     # 老库历史行按非推算
    legacy_cost = db.connect().execute(
        "SELECT cost_known, cost_usd_sum FROM usage_hourly WHERE hour_utc = 1").fetchone()
    assert tuple(legacy_cost) == (0, None)        # 老库历史行无成本（cost_known=0）
    cred_columns = {row[1] for row in db.connect().execute("PRAGMA table_info(credentials)")}
    assert "quota_expiry_ladder" in cred_columns
    # 运维标签列（仅展示筛选，不参与调度）：老库补 '' 后全部凭证无标签
    assert "tags" in cred_columns
    # 额度包明细（展示用）也是本次新增列
    assert "quota_packages" in cred_columns
    # 废弃表被 _MIGRATION_DROPS 清理（schema.sql 删定义对老库无效）
    tables = {row[0] for row in db.connect().execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "checkins" not in tables and "model_cache" not in tables
    # 新增表（(凭证, 模型) 冷却）也由启动时的 schema.sql 全量补建
    assert "credential_model_cooldowns" in tables
    # 运行时配置覆盖表同样是「只加表、不加列」，老库升级后必须存在且可写
    assert "runtime_settings" in tables
    # 凭证级限额护栏表（缺行 = 不受限）也是「只加表」，老库升级后必须建好
    assert "credential_limits" in tables
    db.connect().execute(
        "INSERT INTO credential_limits (credential_id, daily_tokens, updated_at) "
        "VALUES ('cred_1', 100, 1)")
    db.connect().commit()
    assert db.connect().execute(
        "SELECT daily_tokens FROM credential_limits WHERE credential_id = 'cred_1'"
    ).fetchone()[0] == 100
    # 账号体系：users / audit_events 也是「只加表」，老库升级后必须建好
    assert "users" in tables and "audit_events" in tables
    # 补列后可写入、可读出
    db.connect().execute(
        "INSERT INTO credentials (id, provider, data_enc, quota_expiry_ladder, quota_packages,"
        " created_at) VALUES ('cred_1', 'codebuddy', 'x', '[[123, 100.0]]',"
        " '[{\"name\": \"福利积分\"}]', 1)")
    db.connect().commit()
    assert db.connect().execute(
        "SELECT quota_expiry_ladder FROM credentials WHERE id = 'cred_1'").fetchone()[0] == \
        "[[123, 100.0]]"
    assert json.loads(db.connect().execute(
        "SELECT quota_packages FROM credentials WHERE id = 'cred_1'").fetchone()[0]) == \
        [{"name": "福利积分"}]
    # 运行时配置覆盖表：补建后可写入读出（老库升级后管理台立刻可用）
    db.connect().execute(
        "INSERT INTO runtime_settings (key, value, updated_at) "
        "VALUES ('quota_probe_minutes', '15', 1)")
    db.connect().commit()
    stored = db.connect().execute(
        "SELECT value FROM runtime_settings WHERE key = 'quota_probe_minutes'").fetchone()[0]
    assert stored == "15"
    # schema 版本推进到位；补列后新聚合可写
    assert db.connect().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    db.connect().execute(
        "INSERT INTO usage_hourly (hour_utc, username, provider, model, requests,"
        " ok_count, ttfb_sum) VALUES (2, 'u', 'trae', 'm', 1, 1, 120)")
    db.connect().commit()
    apply_schema(db.connect())                          # 二次执行不抛错
    db.close()

def test_migrate_reraises_non_duplicate_errors():
    """迁移中非 duplicate column 的 OperationalError 必须重新抛出。"""
    import pytest as _pytest

    class _FakeConn:
        OperationalError = sqlite3.OperationalError

        def executescript(self, _script):
            pass

        def execute(self, sql):
            if sql.startswith("PRAGMA user_version"):
                return _FakeRow()
            raise sqlite3.OperationalError("no such table")

        def commit(self):
            pass

    class _FakeRow:
        def fetchone(self):
            return (0,)

    with _pytest.raises(sqlite3.OperationalError):
        apply_schema(_FakeConn())

def test_apply_schema_rejects_newer_db(tmp_path):
    """版本回退护栏：库版本高于本程序时拒绝执行，且不改动版本号/数据。"""
    import pytest as _pytest

    from src.db.migrate import SCHEMA_VERSION

    db = Database(tmp_path / "future.sqlite3")
    db.connect().execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    db.connect().commit()
    with _pytest.raises(RuntimeError, match="高于本程序支持的"):
        apply_schema(db.connect())
    # 拒绝时不得把版本号写回本代
    assert db.connect().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1
    db.close()

def test_legacy_db_upgrade_adds_token_columns(tmp_path):
    """老库升级：credentials 无 token 两列时幂等补列。"""
    db = Database(tmp_path / "legacy.sqlite3")
    conn = sqlite3.connect(db.path)
    conn.execute("""
        CREATE TABLE credentials (
            id TEXT PRIMARY KEY, provider TEXT NOT NULL, data_enc BLOB NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1, disabled INTEGER NOT NULL DEFAULT 0,
            disabled_reason TEXT, pinned INTEGER NOT NULL DEFAULT 0,
            health INTEGER, cooling_until INTEGER, err_count INTEGER NOT NULL DEFAULT 0,
            quota_remaining REAL, quota_total REAL, quota_cycle_end INTEGER,
            quota_probed_at INTEGER, created_at INTEGER NOT NULL, added_by TEXT)
    """)
    conn.commit()
    conn.close()
    apply_schema(db.connect())
    columns = {row[1] for row in db.connect().execute("PRAGMA table_info(credentials)")}
    assert {"token_expires_at", "token_issued_at"} <= columns
    # 补列后可写可读，ver 推进
    db.connect().execute(
        "INSERT INTO credentials (id, provider, data_enc, token_expires_at,"
        " token_issued_at, created_at) VALUES ('cred_x', 'codebuddy', 'x', 123, 100, 1)")
    db.connect().commit()
    assert tuple(db.connect().execute(
        "SELECT token_expires_at, token_issued_at FROM credentials WHERE id = 'cred_x'"
    ).fetchone()) == (123, 100)
    from src.db.migrate import SCHEMA_VERSION

    assert db.connect().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    db.close()

def test_legacy_db_upgrade_adds_credit_events_table(tmp_path):
    """老库升级：apply_schema 建出 credit_events（CREATE IF NOT EXISTS 生效）。"""
    db = Database(tmp_path / "legacy.sqlite3")
    conn = sqlite3.connect(db.path)
    conn.execute("CREATE TABLE credentials (id TEXT PRIMARY KEY, provider TEXT NOT NULL,"
                 " data_enc BLOB NOT NULL, created_at INTEGER NOT NULL)")
    conn.commit()
    conn.close()
    apply_schema(db.connect())
    columns = {row[1] for row in db.connect().execute("PRAGMA table_info(credit_events)")}
    assert columns == {"id", "credential_id", "ts", "window_start",
                       "before", "after", "delta", "source", "ledger"}
    from src.db.migrate import SCHEMA_VERSION

    assert db.connect().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    db.close()

def test_legacy_db_upgrade_adds_credit_events_ledger(tmp_path):
    """老库升级：credit_events 无 ledger 时补列，历史流水归入 pool。

    历史行全部由主账（每日池）的探测 diff 产生，`DEFAULT 'pool'` 即为事实，
    不需要回填；积分账流水从补列后的下一轮探测才开始记。
    """
    db = Database(tmp_path / "legacy.sqlite3")
    conn = sqlite3.connect(db.path)
    conn.execute("""
        CREATE TABLE credit_events (
            id TEXT PRIMARY KEY, credential_id TEXT NOT NULL, ts INTEGER NOT NULL,
            window_start INTEGER, before REAL, after REAL, delta REAL,
            source TEXT NOT NULL DEFAULT 'observed')
    """)
    # 老库里已有一条每日池流水：补列后必须保留且取默认 'pool'
    conn.execute("INSERT INTO credit_events (id, credential_id, ts, before, after, delta)"
                 " VALUES ('cre_old', 'cred_1', 1, 100.0, 95.0, -5.0)")
    conn.commit()
    conn.close()

    apply_schema(db.connect())
    columns = {row[1] for row in db.connect().execute("PRAGMA table_info(credit_events)")}
    assert "ledger" in columns
    legacy = db.connect().execute(
        "SELECT ledger, delta FROM credit_events WHERE id = 'cre_old'").fetchone()
    assert tuple(legacy) == ("pool", -5.0)
    # 补列后可写可读：两本账各自成行
    db.connect().execute(
        "INSERT INTO credit_events (id, credential_id, ts, ledger, before, after, delta)"
        " VALUES ('cre_new', 'cred_1', 2, 'credits', 13500.0, 13498.0, -2.0)")
    db.connect().commit()
    rows = db.connect().execute(
        "SELECT ledger FROM credit_events ORDER BY ts").fetchall()
    assert [row[0] for row in rows] == ["pool", "credits"]
    from src.db.migrate import SCHEMA_VERSION

    assert db.connect().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    db.close()


def test_legacy_db_upgrade_adds_credential_tags(tmp_path):
    """老库升级：credentials 无 tags 列时幂等补列，历史行取默认空串。

    `NOT NULL DEFAULT ''`：补列后全部凭证「无标签」，列表筛选默认「全部标签」
    下行为与补列前完全一致，不需要回填。
    """
    db = Database(tmp_path / "legacy.sqlite3")
    conn = sqlite3.connect(db.path)
    conn.execute(
        "CREATE TABLE credentials (id TEXT PRIMARY KEY, provider TEXT NOT NULL,"
        " data_enc BLOB NOT NULL, created_at INTEGER NOT NULL)")
    conn.execute("INSERT INTO credentials (id, provider, data_enc, created_at)"
                 " VALUES ('cred_old', 'trae', x'00', 1)")
    conn.commit()
    conn.close()

    apply_schema(db.connect())
    columns = {row[1] for row in db.connect().execute("PRAGMA table_info(credentials)")}
    assert "tags" in columns
    # 历史行保留且取默认空串（读路径 tags_value('') == []）
    from src.tags import tags_value

    assert tags_value(db.connect().execute(
        "SELECT tags FROM credentials WHERE id = 'cred_old'").fetchone()[0]) == []
    # 补列后可写可读
    db.connect().execute("UPDATE credentials SET tags = '生产,备用' WHERE id = 'cred_old'")
    db.connect().commit()
    assert db.connect().execute(
        "SELECT tags FROM credentials WHERE id = 'cred_old'").fetchone()[0] == "生产,备用"
    from src.db.migrate import SCHEMA_VERSION

    assert db.connect().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    db.close()


def test_legacy_db_upgrade_drops_credits_columns(tmp_path):
    """老库升级：credentials 上遗留的三列积分账被删列，版本推进到 29。

    福利链路下线后积分账成为 CodeArts 唯一的额度来源，直接落在通用列上，
    故老库里这三列必须显式 `ALTER TABLE ... DROP COLUMN`（schema.sql 删定义
    对老库无效）。历史数据（同表其余列）不受影响。
    """
    from src.db.migrate import _MIGRATION_COLUMN_DROPS, SCHEMA_VERSION

    db = Database(tmp_path / "legacy.sqlite3")
    conn = sqlite3.connect(db.path)
    conn.execute("""
        CREATE TABLE credentials (
            id TEXT PRIMARY KEY, provider TEXT NOT NULL, data_enc BLOB NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1, disabled INTEGER NOT NULL DEFAULT 0,
            disabled_reason TEXT, pinned INTEGER NOT NULL DEFAULT 0,
            health INTEGER, cooling_until INTEGER, err_count INTEGER NOT NULL DEFAULT 0,
            quota_remaining REAL, quota_total REAL, quota_cycle_end INTEGER,
            quota_probed_at INTEGER, created_at INTEGER NOT NULL, added_by TEXT,
            quota_credits_remaining REAL, quota_credits_total REAL,
            quota_credits_expiry_ladder TEXT)
    """)
    # 老库里已有一条带积分账的凭证：删列后必须保留（其余列不动）
    conn.execute(
        "INSERT INTO credentials (id, provider, data_enc, quota_remaining,"
        " quota_credits_remaining, quota_credits_total, quota_credits_expiry_ladder,"
        " created_at) VALUES ('cred_old', 'codearts', x'00', 42.0, 13498.72, 13500.0,"
        " '[[123, 40.0]]', 1)")
    conn.commit()
    conn.close()

    apply_schema(db.connect())
    columns = {row[1] for row in db.connect().execute("PRAGMA table_info(credentials)")}
    for _, column in _MIGRATION_COLUMN_DROPS:
        assert column not in columns, column
    assert db.connect().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    # 历史行保留，同表其余列取原值（删列不动数据）
    legacy = db.connect().execute(
        "SELECT provider, quota_remaining FROM credentials WHERE id = 'cred_old'").fetchone()
    assert tuple(legacy) == ("codearts", 42.0)
    # 二次执行幂等：列已不存在 → `no such column` 被忽略，不抛错
    apply_schema(db.connect())
    assert db.connect().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    db.close()


def test_new_db_has_no_credits_columns(tmp_path):
    """新库路径：schema.sql 本就没有三列，删列步骤「列不存在」被静默忽略。"""
    from src.db.migrate import SCHEMA_VERSION

    db = Database(tmp_path / "fresh.sqlite3")
    apply_schema(db.connect())
    columns = {row[1] for row in db.connect().execute("PRAGMA table_info(credentials)")}
    assert "quota_credits_remaining" not in columns
    assert "quota_credits_total" not in columns
    assert "quota_credits_expiry_ladder" not in columns
    assert db.connect().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    db.close()


def test_migrate_reraises_drop_column_errors():
    """删列时的其他 OperationalError（非「no such column」）必须上抛。

    只有列不存在才是预期内的——数据库不可重试之外的任何失败（例如表被锁、
    磁盘错误）都必须让启动失败，不能静默吞掉留下一个「半迁」的库。
    """
    import pytest as _pytest

    class _FakeConn:
        OperationalError = sqlite3.OperationalError

        def executescript(self, _script):
            pass

        def execute(self, sql):
            if sql.startswith("PRAGMA user_version"):
                return _FakeRow()
            if "ADD COLUMN" in sql:      # 补列已存在 → 忽略，继续往下走
                raise sqlite3.OperationalError("duplicate column name: tags")
            if "DROP COLUMN" in sql:     # 删列遇到非「列不存在」的失败 → 上抛
                raise sqlite3.OperationalError("database is locked")
            return None                  # DROP TABLE 等其余语句放行

        def commit(self):  # pragma: no cover - 本用例在 commit 前就抛错
            pass

    class _FakeRow:
        def fetchone(self):
            return (0,)

    with _pytest.raises(sqlite3.OperationalError, match="database is locked"):
        apply_schema(_FakeConn())


def test_legacy_db_upgrade_adds_api_key_policy_columns(tmp_path):
    """老库升级：api_keys 无 provider_binding / allowed_ips 时幂等补列。

    两列都是 `NOT NULL DEFAULT ''`：补列后老 Key 立刻恢复「不限定渠道、
    不限制来源 IP」的原行为，不需要回填。
    """
    db = Database(tmp_path / "legacy.sqlite3")
    conn = sqlite3.connect(db.path)
    conn.execute("""
        CREATE TABLE api_keys (
            id TEXT PRIMARY KEY, username TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
            key_digest TEXT NOT NULL UNIQUE, preview TEXT NOT NULL,
            created_at INTEGER NOT NULL, last_used_at INTEGER)
    """)
    # 老库里已有一条 Key：补列后必须保留且取默认空串
    conn.execute("INSERT INTO api_keys (id, username, key_digest, preview, created_at) "
                 "VALUES ('key_old', 'root', 'digest', 'sk-…old', 1)")
    conn.commit()
    conn.close()

    apply_schema(db.connect())
    columns = {row[1] for row in db.connect().execute("PRAGMA table_info(api_keys)")}
    assert {"provider_binding", "allowed_ips"} <= columns
    legacy = db.connect().execute(
        "SELECT provider_binding, allowed_ips FROM api_keys WHERE id = 'key_old'").fetchone()
    assert tuple(legacy) == ("", "")
    # 补列后可写可读；ver 推进
    db.connect().execute(
        "UPDATE api_keys SET provider_binding = 'trae', allowed_ips = '10.0.0.0/8' "
        "WHERE id = 'key_old'")
    db.connect().commit()
    assert tuple(db.connect().execute(
        "SELECT provider_binding, allowed_ips FROM api_keys WHERE id = 'key_old'").fetchone()
    ) == ("trae", "10.0.0.0/8")
    from src.db.migrate import SCHEMA_VERSION

    assert db.connect().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    db.close()


def test_legacy_db_upgrade_adds_user_policy_columns(tmp_path):
    """老库升级：users 无 allowed_models / quota 时幂等补列（Playground 按人判定用）。

    两列都是 `NOT NULL DEFAULT ''`：补列后老用户立刻回到「不限制」的原行为——
    Playground 的按人判定读的就是它们，缺列会让启动直接失败。
    """
    db = Database(tmp_path / "legacy_users.sqlite3")
    conn = sqlite3.connect(db.path)
    conn.execute("""
        CREATE TABLE users (
            username TEXT PRIMARY KEY, password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'viewer', enabled INTEGER NOT NULL DEFAULT 1,
            must_change_password INTEGER NOT NULL DEFAULT 0,
            session_epoch INTEGER NOT NULL DEFAULT 0,
            activation_digest TEXT, activation_expires_at INTEGER,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, created_by TEXT)
    """)
    conn.execute("INSERT INTO users (username, password_hash, created_at, updated_at) "
                 "VALUES ('alice', 'pbkdf2$old', 1, 1)")
    conn.commit()
    conn.close()

    apply_schema(db.connect())
    columns = {row[1] for row in db.connect().execute("PRAGMA table_info(users)")}
    assert {"allowed_models", "quota"} <= columns
    legacy = db.connect().execute(
        "SELECT allowed_models, quota FROM users WHERE username = 'alice'").fetchone()
    assert tuple(legacy) == ("", "")
    # 补列后可写可读
    db.connect().execute(
        "UPDATE users SET allowed_models = 'glm-*', quota = '{\"rpm\": 3}' "
        "WHERE username = 'alice'")
    db.connect().commit()
    assert tuple(db.connect().execute(
        "SELECT allowed_models, quota FROM users WHERE username = 'alice'").fetchone()) \
        == ("glm-*", '{"rpm": 3}')
    from src.db.migrate import SCHEMA_VERSION

    assert db.connect().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    db.close()
