-- coding2api schema（PROPOSAL §4 定稿 + pin 列）
--
-- 变更纪律：只加不改。
--   新增列 → 这里加定义 + src/db/migrate.py 的 _MIGRATION_COLUMNS 补一条
--   删除表 → 这里删定义 + _MIGRATION_DROPS 补一条（老库不会被 CREATE IF NOT EXISTS 清掉）
--   列注释可以改（不影响存量库结构）
--

-- 注：不建 checkins / model_cache 表——签到去重由 CheckinTask 的当日作用域
-- 集合实现（上游 status 为准）；模型列表是「进程内 TTL 缓存 + DATA_DIR 落盘
-- 快照（model_catalog.json）」，不占表（见 src/api/model_catalog.py）。

-- 用户建表：users.txt（PBKDF2）降级为「引导输入」，启动时一次性导入，
-- 之后 DB 是唯一权威源。ADMIN_USERNAMES env 降级为「引导期角色来源 + 防锁死
-- 兑回」，不再参与每请求鉴权。
-- api_keys.username 由应用层校验存在性，不加外键。

CREATE TABLE IF NOT EXISTS api_keys (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL,
    name          TEXT NOT NULL DEFAULT '',
    key_digest    TEXT NOT NULL UNIQUE,
    preview       TEXT NOT NULL,
    created_at    INTEGER NOT NULL,
    last_used_at  INTEGER,
    -- 多 Key 出口：绑定渠道（见 KNOWN_PROVIDERS 八渠道 | ''=自动）与来源 IP
    -- 白名单（逗号分隔 IP/CIDR，''=不限制）。空值即老库/未设置时的原行为。
    provider_binding TEXT NOT NULL DEFAULT '',
    allowed_ips   TEXT NOT NULL DEFAULT '',
    -- Key 细粒度策略：模型白名单（fnmatch glob，逗号分隔，''=不限制）
    -- 与到期时间（epoch 秒，NULL=永不过期）。
    allowed_models TEXT NOT NULL DEFAULT '',
    expires_at    INTEGER,
    -- Key 级配额（JSON 文本，''=不限制）。结构见 src/auth/access.py
    -- 的 parse_key_quota：daily_requests / daily_tokens / daily_cost_usd /
    -- rpm / tpm / max_concurrency，0 或缺失的键 = 该项关闭。
    -- 存文本而不是拆列：配额项是「可增可减的策略集合」，拆列会让每次加一项
    -- 都要走迁移；读路径宽容解析，坏行退化为「不限制」而不是崩掉出口。
    quota         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS credentials (
    id               TEXT PRIMARY KEY,
    provider         TEXT NOT NULL,          -- 见 KNOWN_PROVIDERS（八渠道）
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
    quota_cycle_end  INTEGER,                      -- 最早到期 epoch；渠道无此信息为 NULL
    quota_expiry_ladder TEXT,                      -- 到期阶梯 JSON [[epoch, 剩余积分]]；无到期信息为 NULL
    quota_packages   TEXT,                      -- 额度包明细 JSON [{"name","total","used","end"}]，仅展示
    quota_probed_at  INTEGER,
    token_expires_at INTEGER,                   -- access token 到期 epoch；NULL=老库未回填，0=未知
    token_issued_at  INTEGER,                   -- access token 签发 epoch（JWT iat）：进度条满量程与「最后续期」
    growth_last_run_at INTEGER,                    -- 成长中心最近一轮执行时间（仅 CodeBuddy）
    growth_last_result TEXT,                       -- 该轮一行中文汇报
    tags             TEXT NOT NULL DEFAULT '',     -- 逗号分隔的运维标签（纯展示筛选，不参与调度）
    created_at       INTEGER NOT NULL,
    added_by         TEXT                          -- 应用层校验存在于 users 表
);

CREATE INDEX IF NOT EXISTS idx_credentials_provider ON credentials(provider);

-- 凭证级限额护栏（每凭证可选，缺行 = 不受限）。
--
-- 四个护栏与 src/engine/limits.py 的 evaluate 一一对应，全部以「当日」
-- （北京时间自然日）为口径，0 / NULL 表示该护栏关闭：
--   keep_credits      账户剩余积分 ≤ 该值时停用（给号留底，不跑空）
--   daily_tokens      当日 token（输入+输出）达到上限时停用
--   daily_credits     当日 credit 达到上限时停用
--   per_model_tokens  JSON {模型: 当日 token 上限}，某模型达到上限时只停该模型
--
-- 为什么单独建表：限额是「凭证的可选附加策略」，大多数凭证没有；塞进
-- credentials 会加一批恒 NULL 的列，且限额变更频率与凭证主体不同。
CREATE TABLE IF NOT EXISTS credential_limits (
    credential_id    TEXT PRIMARY KEY,
    keep_credits     REAL,
    daily_tokens     INTEGER,
    daily_credits    REAL,
    per_model_tokens TEXT NOT NULL DEFAULT '{}',   -- JSON：{模型名: token 上限}
    updated_at       INTEGER NOT NULL,
    updated_by       TEXT
);

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
    credit_estimated INTEGER NOT NULL DEFAULT 0,  -- credit 是否本服务推算（1=推算，展示标 ≈）
    -- 成本估算（OpenRouter 刊例价 × 当时汇率，写入时定值）：NULL=未匹配到定价或未上报 token。
    -- 两列同生同灭；历史行不随汇率/价表变化而重算。
    cost_usd         REAL,
    cost_cny         REAL,
    latency_ms       INTEGER,             -- 端到端耗时（排队+首字+生成），非网络延迟
    ttfb_ms          INTEGER,             -- 首字延迟（请求开始到首个内容帧）
    -- 请求 ID（响应头 X-Request-ID 同值）：把「用户报的一个请求」与这行明细、
    -- 以及与上游日志对齐。NULL=老库历史行（未回填）；入站未带头时自生成。
    request_id       TEXT,
    -- 发起请求的 API Key id（api_keys.id）。NULL = 老库历史行、Cookie 会话调用
    -- 或未带 Key 的调用。只由鉴权层写入（请求头不可信），供按 Key 计量与自助
    -- 用量查询；`usage_hourly` 刻意不加该维度（会炸维度，聚合走明细表）。
    api_key_id       TEXT
);

CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_events(ts);
CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_events(username, ts);
-- `idx_usage_request`（失败归因按 request_id 关联成功行）不在这里建：
-- request_id 是老库后补的列，`executescript` 先于补列执行会报
-- "no such column"，只能走 `migrate.py` 的 `_MIGRATION_INDEXES`。

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
    credit_estimated_known INTEGER NOT NULL DEFAULT 0,  -- 其中推算值条数（>0 时该值标 ≈）
    -- 成本估算聚合：USD/CNY 两列由同一条明细同时写入，故共用一个 cost_known 计数；
    -- cost_known=0 时两列不可信（该小时没有任何可定价明细），查询回 None。
    cost_usd_sum  REAL,
    cost_cny_sum  REAL,
    cost_known    INTEGER NOT NULL DEFAULT 0,
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

-- 变动流水：额度探测写回时比对余额，每本账各增记一条。
--
-- 为什么靠 diff：签到 / 成长中心的上游接口普遍不打日志，拿不到「这次动作
-- 加了多少分」。所以本表记的是**两次探测之间的净变化**，不是动作归因——
-- source 只表达归因已知度（observed=常规探测区间 / sync=首次建立基线），
-- 绝不写「签到 +5」这种上游并未告知的结论。
--
-- `ledger` 区分两本账：`pool`＝每日 token 池（福利模型用，CodeArts 之外
-- 各渠道的主账）、`credits`＝积分账（CodeArts 内置模型用）。两本账数值
-- 量级与语义都不同（每日池约 1000、积分账约 14000），混在一个列表里会被
-- 读成同一本账的连续读数，故查询侧按它分开读。
--
-- before/after 可空：只在两端都拿得到数值时才记 delta；任一为 NULL 时
-- 本行说明「变化无法量化」（如探测失败后恢复），不猜 0。
CREATE TABLE IF NOT EXISTS credit_events (
    id             TEXT PRIMARY KEY,
    credential_id  TEXT NOT NULL,
    ts             INTEGER NOT NULL,             -- 观测时刻（本次探测写回时间）
    window_start   INTEGER,                      -- 变化覆盖起点（上次成功探测时刻）
    before         REAL,                         -- 上次观测余额（可空）
    after          REAL,                         -- 本次观测余额（可空）
    delta          REAL,                         -- after - before，仅两端可算时非空
    source         TEXT NOT NULL DEFAULT 'observed',
                   -- observed=两次探测间净变化 | sync=首次建立基线（无对照）
    ledger         TEXT NOT NULL DEFAULT 'pool'
                   -- pool=每日 token 池（福利模型）| credits=积分账（内置模型）
);

CREATE INDEX IF NOT EXISTS idx_credit_events_cred_ts
    ON credit_events(credential_id, ts);

-- (凭证, 模型) 级冷却：模型级限流（6004）与「该后端无此模型」（11102）负缓存。
--
-- 必须与 credentials.cooling_until 分开：6004 只影响触发的那个模型，
-- 写进账号级冷却会让同账号的其他模型一起不可用（实测语义）。
-- hits 供指数退避（模型冷却翻倍封顶 2h；负缓存 6h→24h）。
CREATE TABLE IF NOT EXISTS credential_model_cooldowns (
    credential_id  TEXT NOT NULL,
    model          TEXT NOT NULL,
    cooling_until  INTEGER NOT NULL,
    hits           INTEGER NOT NULL DEFAULT 0,
    reason         TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (credential_id, model)
);

CREATE INDEX IF NOT EXISTS idx_model_cooling_until
    ON credential_model_cooldowns(cooling_until);

-- 主动探活结论：(渠道, 模型) 级，与生产冷却/统计**完全隔离**。
--
-- 为什么单独建表：`credential_model_cooldowns` 是「调度要不要避开」的权威
-- 状态，由真实请求的失败驱动；探活只是「再试一次看恢复没有」的观测。把
-- 探活结论写进冷却表会让一次探测直接改变选号结果（探测失败 ≠ 上游拒绝），
-- 而写进 usage_events 会污染用量与成本统计。故独立成表，只供管理台展示 +
-- 「探活成功就清负缓存」这一个动作。
--
-- `state`：ok（探活成功）/ failed（上游明确不认这个模型）/ inconclusive
-- （额度、限流、网络等与模型无关的失败——**绝不能**据此禁用）。
-- `manual`：人工接管标记（非空 = 跳过自动探活），见 src/api/admin_model_availability.py。
-- `failed` + 同 (渠道, 模型) 仍有生效中的 `credential_model_cooldowns`
-- （reason='blocked'）时，该 (渠道, 模型) 会被模型列表暂时摘掉；到期口径
-- 就是那条负缓存的 cooling_until，见 src/api/models.py::merged_entries。
CREATE TABLE IF NOT EXISTS model_probes (
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT '',
    failures      INTEGER NOT NULL DEFAULT 0,
    last_probe_at INTEGER NOT NULL DEFAULT 0,
    last_ok_at    INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT NOT NULL DEFAULT '',
    manual        TEXT NOT NULL DEFAULT '',
    updated_at    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (provider, model)
);

-- 运行时配置覆盖：只存「管理台改过」的 key，未出现的 key 回落 env。
--
-- 为什么单独建表而不是加列到别的表：键集合随版本演进（新增可热更项不
-- 需要迁移），且 key/value 都是文本，值的类型与取值范围由
-- src/runtime_settings.py 的 HOT_SETTINGS 白名单校验——表本身不做约束，
-- 白名单外/类型非法的行在读取时被忽略并记日志，不让一行坏数据把服务拖崩。
-- 注意：这里存的是**覆盖意图**，不是权威值；env 仍是默认值来源。
CREATE TABLE IF NOT EXISTS runtime_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,        -- 统一以文本存储，读时按白名单类型解析
    updated_at  INTEGER NOT NULL
);

-- 管理台用户账号。启动时由 users.txt 导入一次，之后本表是唯一权威源。
--
-- 为什么建表：users.txt 只能手工编辑 + 重启才生效，做不到「禁用立即踢会话」
-- 「改角色即时生效」「登录与操作留痕」。本表把身份变成可运维的数据。
--
-- 两个安全相关的列需要解释：
--   session_epoch  会话吊销。签名 Cookie 不落库，无法逐条作废，于是改密/禁用/
--                  改角色时 +1；Cookie 里带上签发时的 epoch，每请求比对，
--                  不等即 401。代价是这几个动作会让该用户所有会话重新登录
--                  ——这是有意的：降级必须立刻可信。
--   must_change_password  首登强制改密。为 1 时除放行清单外的端点全部 403。
CREATE TABLE IF NOT EXISTS users (
    username             TEXT PRIMARY KEY,
    password_hash        TEXT NOT NULL,           -- 复用 users.txt 的 PBKDF2 格式
    role                 TEXT NOT NULL DEFAULT 'viewer',  -- admin | operator | viewer
    enabled              INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    session_epoch        INTEGER NOT NULL DEFAULT 0,
    -- 一次性激活令牌：只存摘要，明文仅在创建响应里回显一次，与 API Key 同一心智模型。
    -- 用户带 token 访问 /activate 自设密码；成功即清空。NULL = 无待激活令牌。
    activation_digest    TEXT,
    activation_expires_at INTEGER,
    created_at           INTEGER NOT NULL,
    updated_at           INTEGER NOT NULL,
    created_by           TEXT                     -- 建号者用户名；引导导入为 NULL
);

CREATE INDEX IF NOT EXISTS idx_users_role_enabled ON users(role, enabled);
CREATE INDEX IF NOT EXISTS idx_users_activation ON users(activation_digest);

-- 审计流水：登录、账号变动、凭证写操作。
--
-- 为什么单独建表而不是只打日志：日志会滚动丢失、无法按 actor 过滤、也不能在
-- 管理台展示。detail 只放可读短句，绝不写密码 / 令牌 / 凭证明文。
-- 失败登录同样入库（ok=0）——「谁在什么时候试了谁的账号」正是审计的价值。
CREATE TABLE IF NOT EXISTS audit_events (
    id      TEXT PRIMARY KEY,
    ts      INTEGER NOT NULL,
    actor   TEXT NOT NULL,                        -- 操作者；失败登录为被尝试的用户名
    action  TEXT NOT NULL,                        -- 见 src/audit/actions.py 枚举
    target  TEXT,                                 -- 被动方（用户名 / 凭证 id）
    detail  TEXT NOT NULL DEFAULT '',
    ip      TEXT,
    ok      INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events(ts);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_events(actor, ts);

-- 运维告警事件：后台周期评估四类风险（池耗尽 / 任务连续失败 / token
-- 临近到期 / 上游错误率骤升），命中即落一行，供管理台「站内告警记录」回看。
--
-- 为什么落库而不是像 TaskStatusStore 只留内存：告警的价值恰在「错过的那段
-- 时间发生了什么」——夜里池子耗尽、某任务连挂几轮，运维醒来要能看见，而
-- 任务 last-run 是进程内、重启就丢。保留期由 retention 任务按明细同一策略清理。
-- (rule, scope) 是静默去重键：同一条告警在静默窗内不重复落库，避免每轮刷屏。
CREATE TABLE IF NOT EXISTS alert_events (
    id             TEXT PRIMARY KEY,
    ts             INTEGER NOT NULL,
    rule           TEXT NOT NULL,          -- pool_empty | task_failed | token_expiring | error_rate
    severity       TEXT NOT NULL,          -- critical | warning
    scope          TEXT NOT NULL DEFAULT '', -- 具体对象：pool / 任务 key / 凭证 id / 渠道
    message        TEXT NOT NULL,
    detail         TEXT NOT NULL DEFAULT '', -- JSON 补充数据（计数 / 到期时间等）
    delivered      INTEGER NOT NULL DEFAULT 0, -- webhook 是否投递成功（0=未配置或失败）
    delivery_error TEXT,
    -- 恢复通知：该 (规则, 对象) 的告警在此时刻被观测到恢复（NULL=仍在告警中）。
    -- 迟滞状态机在内存里维护「连续命中/连续恢复」计数，只有确认恢复才写这里，
    -- 并外推一条 resolved 通知（见 src/tasks/alerting.py）。
    resolved_at    INTEGER
);

CREATE INDEX IF NOT EXISTS idx_alert_ts ON alert_events(ts);
CREATE INDEX IF NOT EXISTS idx_alert_rule_scope ON alert_events(rule, scope);
-- 注：resolved_at 上的索引不在这里，因为它引用的列本身是迁移补的
-- （见 src/db/migrate.py 的 _MIGRATION_INDEXES）——老库执行到本脚本时该列
-- 尚未补上，CREATE INDEX 会 no such column。新库由迁移循环统一建。
