-- coding2api schema（PROPOSAL §5 定稿 + pin 列）
--
-- 变更纪律：只加不改。
--   新增列 → 这里加定义 + src/db/migrate.py 的 _MIGRATION_COLUMNS 补一条
--   删除表 → 这里删定义 + _MIGRATION_DROPS 补一条（老库不会被 CREATE IF NOT EXISTS 清掉）
--   列注释可以改（不影响存量库结构）
--

-- 注：不建 checkins / model_cache 表——签到去重由 CheckinTask 的当日作用域
-- 集合实现（上游 status 为准），模型列表是进程内 TTL 缓存，重启即重建。

-- 用户不建表：users.txt（PBKDF2）是唯一源，角色走 ADMIN_USERNAMES env。
-- api_keys.username 由应用层校验存在性，不加外键。

CREATE TABLE IF NOT EXISTS api_keys (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL,
    name          TEXT NOT NULL DEFAULT '',
    key_digest    TEXT NOT NULL UNIQUE,
    preview       TEXT NOT NULL,
    created_at    INTEGER NOT NULL,
    last_used_at  INTEGER
);

CREATE TABLE IF NOT EXISTS credentials (
    id               TEXT PRIMARY KEY,
    provider         TEXT NOT NULL,          -- codebuddy | trae
    nickname         TEXT NOT NULL DEFAULT '',
    data_enc         BLOB NOT NULL,          -- Fernet 加密的凭证 JSON
    enabled          INTEGER NOT NULL DEFAULT 1,   -- 用户软开关
    disabled         INTEGER NOT NULL DEFAULT 0,   -- session 死亡硬禁用
    disabled_reason  TEXT,
    pinned           INTEGER NOT NULL DEFAULT 0,   -- 手动指定当前凭证
    health           INTEGER,                      -- NULL=unknown；0-100=known；-1=exhausted
    cooling_until    INTEGER,
    err_count        INTEGER NOT NULL DEFAULT 0,
    quota_remaining  REAL,
    quota_total      REAL,
    quota_cycle_end  INTEGER,                      -- 最早到期 epoch；TRAE 为 NULL
    quota_expiry_ladder TEXT,                      -- 到期阶梯 JSON [[epoch, 剩余积分]]；TRAE 为 NULL
    quota_packages   TEXT,                      -- 额度包明细 JSON [{"name","total","used","end"}]，仅展示
    quota_probed_at  INTEGER,
    growth_last_run_at INTEGER,                    -- 成长中心最近一轮执行时间（仅 CodeBuddy）
    growth_last_result TEXT,                       -- 该轮一行中文汇报
    created_at       INTEGER NOT NULL,
    added_by         TEXT                          -- 应用层校验存在于 users.txt
);

CREATE INDEX IF NOT EXISTS idx_credentials_provider ON credentials(provider);

CREATE TABLE IF NOT EXISTS usage_events (
    id               TEXT PRIMARY KEY,
    ts               INTEGER NOT NULL,
    username         TEXT NOT NULL,
    provider         TEXT NOT NULL,
    credential_id    TEXT,
    model            TEXT NOT NULL,
    ok               INTEGER NOT NULL,
    error_type       TEXT,
    input_tokens     INTEGER,
    output_tokens    INTEGER,
    reasoning_tokens INTEGER,
    cached_tokens    INTEGER,                  -- 输入中命中缓存的 token（上游可选，NULL=未上报）
    credit           REAL,                     -- 上游可选字段，两边都经常为 NULL
    latency_ms       INTEGER,             -- 端到端耗时（排队+首字+生成），非网络延迟
    ttfb_ms          INTEGER              -- 首字延迟（请求开始到首个内容帧）
);

CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_events(ts);
CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_events(username, ts);

CREATE TABLE IF NOT EXISTS usage_hourly (
    hour_utc      INTEGER NOT NULL,
    username      TEXT NOT NULL,
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    requests      INTEGER NOT NULL DEFAULT 0,
    ok_count      INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,      -- 命中缓存的输入 token 之和
    cached_known  INTEGER NOT NULL DEFAULT 0,      -- 上报过 cached_tokens 的条数（=0 时该值不可信）
    credit_sum    REAL,
    credit_known  INTEGER NOT NULL DEFAULT 0,
    latency_sum   INTEGER NOT NULL DEFAULT 0,
    ttfb_sum      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (hour_utc, username, provider, model)
);

-- 成长中心运行记录（仅 CodeBuddy 有该活动）。
-- 不建明细子表：一轮的 7 类领取合并成一行 report 文本（人话），
-- 界面直接展示，也便于「今天这个号到底领到了什么」一句话回答。
CREATE TABLE IF NOT EXISTS growth_events (
    id             TEXT PRIMARY KEY,
    credential_id  TEXT NOT NULL,
    ts             INTEGER NOT NULL,
    ok             INTEGER NOT NULL,
    session_dead   INTEGER NOT NULL DEFAULT 0,   -- 登录态失效：需要重新登录（硬禁用）
    report         TEXT NOT NULL DEFAULT '',     -- 一行中文汇报
    credit         REAL,                         -- 本轮累计获得积分（可缺）
    energy         INTEGER,
    streak_days    INTEGER,
    trigger        TEXT NOT NULL DEFAULT 'auto'  -- auto=定时 | manual=管理台手动
);

CREATE INDEX IF NOT EXISTS idx_growth_cred_ts ON growth_events(credential_id, ts);
