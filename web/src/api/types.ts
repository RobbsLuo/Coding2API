/**
 * 后端 API 契约（并行开发的唯一事实来源）。
 *
 * 后端已完成并测试通过（484 测试 / 100% 覆盖），本文件与 src/main.py 的路由一一对应。
 * 各页面只依赖本文件，不要各自猜测字段名。
 */

export type Provider = "codebuddy" | "trae";

/** 健康度三态：null = 未探测；-1 = 已耗尽；0-100 = 已知剩余百分比。 */
export type Health = number | null;

/** 额度包明细（管理台悬浮展示）：各渠道的积分/权益包。 */
export interface QuotaPackage {
  /** 包名：CodeBuddy 为 PackageName；TRAE 为福利积分/每月登录赠送/签到奖励等，可能为空 */
  name: string;
  total: number;
  used: number;
  /** 到期 epoch；无到期信息为 null */
  end: number | null;
}

/** 一条 (凭证, 模型) 级冷却：模型级限流或「该渠道无此模型」负缓存。 */
export interface ModelCooldown {
  model: string;
  /** 冷却截止 epoch */
  cooling_until: number;
  /** 连续命中次数（退避升级用） */
  hits: number;
  /** "model" = 模型级限流；"blocked" = 该账号无此模型的负缓存 */
  reason: string;
}

export interface Credential {
  id: string;
  provider: Provider;
  nickname: string;
  /** 软开关：1=参与对话选号；0=暂停（只摘对话流量，后台任务照常） */
  enabled: 0 | 1;
  /** session 死亡硬禁用 */
  disabled: 0 | 1;
  disabled_reason: string | null;
  /** 手动指定（全局唯一一条为 1） */
  pinned: 0 | 1;
  health: Health;
  cooling_until: number | null;
  err_count: number;
  quota_remaining: number | null;
  quota_total: number | null;
  /** 仅 CodeBuddy 有周期；TRAE 为 null */
  quota_cycle_end: number | null;
  /** 调度主窗口（36h）内即将到期的积分（与选号排序第一级同源）；无到期信息的渠道为 null */
  quota_expiring_credits: number | null;
  /** 调度次窗口（7 天）内即将到期的积分（主窗口打平时才参与排序的第二级）；同上 */
  quota_expiring_credits_secondary: number | null;
  /** 套餐到期阶梯 [[到期 epoch, 该套餐剩余积分]]，仅 CodeBuddy；TRAE 为 null */
  quota_expiry_ladder: [number, number][] | null;
  /** 额度包明细（含包名，仅供展示）；未探测或上游未提供时为 null */
  quota_packages: QuotaPackage[] | null;
  /** 生效中的模型级冷却（模型级限流/负缓存）；无则为空数组 */
  model_cooldowns: ModelCooldown[];
  quota_probed_at: number | null;
  /** access token 到期 epoch（秒）；0 = 未知（渠道没给到期信息，也可能是 JWT 不可解析） */
  token_expires_at: number;
  /** access token 签发 epoch（秒，JWT iat）；0 = 未知。进度条满量程 = 它到到期时间的间隔 */
  token_issued_at: number;
  /** 成长中心最近一轮执行时间（仅 CodeBuddy；老库升级前为 null） */
  growth_last_run_at: number | null;
  /** 该轮一行中文汇报 */
  growth_last_result: string | null;
  created_at: number;
  added_by: string | null;
}

export interface ApiKey {
  id: string;
  username: string;
  name: string;
  /** 脱敏预览，形如 sk-…Ab3d */
  preview: string;
  created_at: number;
  last_used_at: number | null;
  /** 绑定的渠道（codebuddy | trae）；空串 = 自动 */
  provider_binding: string;
  /** 来源 IP 白名单（规范化后的 IP/CIDR 逗号分隔）；空串 = 不限制 */
  allowed_ips: string;
}

/** 创建成功时才有明文（只返回一次） */
export interface ApiKeyCreated extends ApiKey {
  api_key: string;
}

export interface StatsOverview {
  requests: number;
  ok_count: number;
  success_rate: number | null;
  input_tokens: number;
  output_tokens: number;
  reasoning_tokens: number;
  /** 输入中命中缓存的 token；任一明细上报过才非 null */
  cached_tokens: number | null;
  /** 渠道可选字段，两边都经常为 null */
  credit: number | null;
  /** 平均端到端耗时（排队 + 首字 + 生成），非网络延迟 */
  avg_latency_ms: number | null;
  /** 平均首字延迟（TTFB）：请求开始到首个内容帧，接近真实体感延迟 */
  avg_ttfb_ms: number | null;
}

export interface ProviderStats {
  provider: Provider;
  requests: number;
  ok_count: number;
  input_tokens: number;
  output_tokens: number;
  credit: number | null;
}

/** 图表指标：请求次数 / token 消耗 / 平均耗时 / 平均首字延迟。 */
export type StatsMetric = "requests" | "tokens" | "latency" | "ttfb";

/** 小时粒度时间序列点（usage_hourly 聚合，跨 model 汇合）。值语义随 metric 变化。 */
export interface TimelinePoint {
  /** UTC 秒，对齐到小时起点 */
  hour: number;
  codebuddy: number;
  trae: number;
}

/** 按模型趋势：每小时各 Top N 模型的指标值（模型名为动态键）。 */
export interface ModelTimelinePoint {
  hour: number;
  [model: string]: number;
}

export interface ModelTimelineResponse {
  models: string[];
  points: ModelTimelinePoint[];
}

/** 逐请求明细（usage_events，仅保留 90 天）。 */
export interface UsageEventRow {
  rowid: number;
  ts: number;
  username: string;
  provider: string;
  model: string;
  /** 本次请求命中的凭证 ID（预热失败等无凭证场景为 null） */
  credential_id: string | null;
  /** 凭证昵称（凭证已删除时为 null，回退展示 credential_id 前缀） */
  credential_name: string | null;
  ok: number;
  error_type: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cached_tokens: number | null;
  credit: number | null;
  /** 端到端耗时（排队 + 首字 + 生成），非网络延迟 */
  latency_ms: number | null;
  /** 首字延迟（TTFB）：请求开始到首个内容帧；无帧（如预热失败）时为 null */
  ttfb_ms: number | null;
}

export interface StatsEventsResponse {
  events: UsageEventRow[];
  /** 下一页游标：本页最小 rowid；null 表示到底 */
  next_before: number | null;
}

/** 三角色（B5）：与后端 src/auth/rbac.py 的 ROLE_* 字面量一致。 */
export type Role = "admin" | "operator" | "viewer";

export const ROLE_LABELS: Record<Role, string> = {
  admin: "管理员",
  operator: "操作员",
  viewer: "只读",
};

export const ROLE_OPTIONS: { value: Role; label: string }[] = [
  { value: "admin", label: "管理员" },
  { value: "operator", label: "操作员" },
  { value: "viewer", label: "只读" },
];

export interface SessionInfo {
  username: string;
  is_admin: boolean;
  role: Role;
  /** 首登/被重置后必须改密：为 true 时前端弹不可关闭的改密对话框 */
  must_change_password: boolean;
}

/** 管理台用户（GET /api/users 的一行）。**绝不包含密码哈希**。 */
export interface UserRow {
  username: string;
  role: Role;
  enabled: boolean;
  must_change_password: boolean;
  created_at: number;
  updated_at: number;
  created_by: string | null;
  /** 是否有待使用的一次性激活令牌（摘要本身不会下发） */
  pending_activation: boolean;
}

/** 创建/重置用户时返回：明文令牌只出现这一次。 */
export interface ActivationIssued {
  username: string;
  activate_token: string;
  expires_at: number;
  role?: Role;
}

/** 审计流水一行（audit_events 表）。 */
export interface AuditEvent {
  id: string;
  ts: number;
  actor: string;
  action: string;
  target: string | null;
  detail: string;
  ip: string | null;
  ok: 0 | 1;
}

export interface AuditResponse {
  events: AuditEvent[];
  /** 可选的 action 枚举（与后端 src/audit/actions.py 同源） */
  actions: string[];
  /** action → 中文标签 */
  labels: Record<string, string>;
}

export interface ActivationDescribe {
  username: string;
  valid: boolean;
}

export interface CredentialsResponse {
  credentials: Credential[];
  /** 调度主到期排序窗口（秒）；与列表中各凭证的到期积分同一口径 */
  expiry_window_seconds: number;
  /** 调度次到期排序窗口（秒）；主窗口打平时才参与排序 */
  expiry_secondary_window_seconds: number;
  /** token 到期预警阈值（秒）：剩余低于它时前端标红 */
  token_expiry_warning_seconds: number;
  viewer: string;
  is_admin: boolean;
}

export interface UpstreamAuthStart {
  flow: "poll" | "callback";
  state: string;
  auth_url: string | null;
  interval: number | null;
}

export interface UpstreamAuthPoll {
  status: "pending" | "success";
  credential_id?: string;
}

/** 后端保证是稳定的机器可读枚举，不是 Python 异常类名。 */
export type ProbeFailureReason =
  | "credential_rejected"
  | "rate_limited"
  | "upstream_unavailable"
  | "upstream_rejected"
  | "upstream_response_invalid"
  | "upstream_timeout"
  | "unknown_error";

export interface ProbeResult {
  probed: boolean;
  remaining?: number;
  total?: number;
  reason?: ProbeFailureReason;
  /** 原始错误摘要，仅用于排查；界面不应直接展示给普通用户 */
  detail?: string;
}

export interface CheckinStatus {
  active: boolean;
  today_checked_in: boolean;
  /** 连续签到天数；渠道不给时为 null */
  streak_days: number | null;
  /** 今日/累计积分；渠道不给时为 null */
  today_credit: number | null;
  total_credits: number | null;
  activity_name: string;
  is_streak_day: boolean;
}

/** 成长中心单步结果：done=有收获；idle=无事可做（不是错误）；skipped=开关关闭；failed 才是问题 */
export type GrowthStepStatus = "done" | "idle" | "skipped" | "failed";

export interface GrowthStepResult {
  name: string;
  status: GrowthStepStatus;
  detail: string;
  credit: number | null;
}

export interface GrowthRunResult {
  ok: boolean;
  /** 一行中文汇报 */
  report: string;
  credit: number | null;
  energy: number | null;
  streak_days: number | null;
  /** 登录态失效：需要重新登录桌面端 */
  session_dead: boolean;
  steps: GrowthStepResult[];
}

/** 活跃上报（B1.7）单次结果 */
export interface ActivityReportResult {
  ok: boolean;
  message: string;
}

/** 成长中心运行记录（growth_events 表一行） */
export interface GrowthEvent {
  id: string;
  credential_id: string;
  ts: number;
  ok: 0 | 1;
  session_dead: 0 | 1;
  report: string;
  credit: number | null;
  energy: number | null;
  streak_days: number | null;
  /** auto=定时任务；manual=管理台手动执行 */
  trigger: string;
}

/** 积分变动流水（B3.4）：两次额度探测之间的**净变化**，不是动作归因。 */
export interface CreditEvent {
  id: string;
  credential_id: string;
  /** 观测时刻（本次探测写回时间） */
  ts: number;
  /** 变化覆盖起点（上次成功探测时刻）；首次建立基线时为 null */
  window_start: number | null;
  before: number | null;
  after: number | null;
  /** after - before；两端任一未知时为 null（不猜成 0） */
  delta: number | null;
  /** observed=两次探测间净变化；sync=首次建立基线（无对照） */
  source: string;
}

export interface CheckinResult {
  ok: boolean;
  credit: number | null;
  code: number | null;
  message: string;
  /** 渠道把「已签到」返回成 400 + code=10001，这不是错误 */
  already_checked_in: boolean;
  /** 渠道可选回填的活动状态（CodeBuddy 有；TRAE 为 null） */
  status: CheckinStatus | null;
}

export interface ModelInfo {
  id: string;
  object: string;
  owned_by: string;
  providers: Provider[];
  /** 以下为可选元数据：上游提供才透传（见 src/api/models.py 的 _META_FIELDS） */
  credit_rate?: number;
  max_input_tokens?: number;
  max_output_tokens?: number;
  supports_images?: boolean;
  supports_tool_call?: boolean;
  supports_reasoning?: boolean;
  default_effort?: number;
  /** 双上游都提供同一模型时才有：按渠道的倍率差异 */
  by_provider?: Record<string, { credit_rate?: number }>;
}

/**
 * 一条可热更运行时配置（GET/PUT /api/settings）。
 *
 * 关键语义：`value` 是**当前生效值**（DB 覆盖 > .env），`default` 是 .env
 * 默认值，`overridden` 表示该项正被 DB 覆盖。界面必须同时展示这三个，
 * 否则用户改了 .env 却发现不生效，会以为是 bug——实际是 DB 覆盖在起作用。
 */
export interface RuntimeSetting {
  key: string;
  /** 对应的环境变量名（大写），供用户对照 .env */
  env_name: string;
  label: string;
  description: string;
  /** "int" | "float" | "bool" | "str"（后端类型名） */
  kind: string;
  value: string | number | boolean;
  default: string | number | boolean;
  overridden: boolean;
  /** 所属后台任务 key（见 TaskStatus.key）；null = 网关/调度等非任务配置 */
  task: string | null;
}

export interface SettingsResponse {
  settings: RuntimeSetting[];
  /** 被 DB 覆盖的项数（0 表示全部来自 .env） */
  overridden: number;
}

/**
 * 一个后台任务的运行态（GET /api/tasks，进程内、重启归零）。
 *
 * `last_*` 只描述**真实执行**：签到未到点这类 no-op 轮次不覆盖它们，否则
 * 「上次执行」会谎报成刚刚跑过，而当天其实一次都没签。`interval_seconds`
 * 是当前生效值（热更后立刻反映）。
 */
export interface TaskStatus {
  key: string;
  name: string;
  description: string;
  interval_seconds: number;
  /** 开关类任务（目前只有活跃上报）的当前状态；其余恒为 true */
  enabled: boolean;
  /** 本进程内真实执行过的轮数（no-op 不计） */
  runs: number;
  last_started_at: number | null;
  last_finished_at: number | null;
  last_ok: boolean | null;
  last_report: Record<string, unknown> | null;
  last_error: string | null;
}

export interface TasksResponse {
  tasks: TaskStatus[];
  /** 服务端当前时间（epoch 秒）：算「距今多久」用服务端时钟，避免浏览器偏移 */
  server_time: number;
}
