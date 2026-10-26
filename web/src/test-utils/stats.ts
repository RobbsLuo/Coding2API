/** StatsPage 测试共享的样例数据。 */
export const ADMIN = { username: "root", is_admin: true } as const;
export const READER = { username: "alice", is_admin: false } as const;

export const OVERVIEW = {
  requests: 120,
  ok_count: 114,
  success_rate: 0.95,
  input_tokens: 1000,
  output_tokens: 2000,
  reasoning_tokens: 300,
  cached_tokens: 400,
  credit: 12.5,
  credit_estimated: false,
  cost_usd: 1.84,
  cost_cny: 12.34,
  avg_latency_ms: 850,
  avg_ttfb_ms: 220,
  tps: 94.1,
  tps_visible: 52.0,
  tps_estimated: false,
};

export const PROVIDERS = {
  providers: [
    { provider: "trae", requests: 70, ok_count: 68, input_tokens: 600, output_tokens: 1200, cached_tokens: 250, credit: 5.25, credit_estimated: true, cost_usd: 0.5, cost_cny: 3.35, avg_latency_ms: 8200, avg_ttfb_ms: 2400, tps: 88.0, tps_visible: 61.0, tps_estimated: false, p50_latency_ms: 6200, p50_ttfb_ms: 1800, p90_latency_ms: 15400, p90_ttfb_ms: 7200, p95_latency_ms: 22170, p95_ttfb_ms: 10864, p99_latency_ms: 28900, p99_ttfb_ms: 14200},
    { provider: "codebuddy", requests: 50, ok_count: 46, input_tokens: 400, output_tokens: 800, cached_tokens: 150, credit: 12.5, credit_estimated: false, cost_usd: 1.34, cost_cny: 8.99, avg_latency_ms: 15300, avg_ttfb_ms: 3100, tps: 120.5, tps_visible: 84.0, tps_estimated: true, p50_latency_ms: 11800, p50_ttfb_ms: 2200, p90_latency_ms: 20100, p90_ttfb_ms: 8600, p95_latency_ms: 22227, p95_ttfb_ms: 10882, p99_latency_ms: 27500, p99_ttfb_ms: 13100},
  ],
};

export const MODELS = {
  models: [
    { model: "glm-5.2", requests: 40, ok_count: 38, input_tokens: 300, output_tokens: 900, cached_tokens: 100, credit: 8.0, credit_estimated: false, cost_usd: 0.8, cost_cny: 5.36, avg_latency_ms: 4200, avg_ttfb_ms: 900, tps: 102.0, tps_visible: 58.0, tps_estimated: false, p50_latency_ms: 2600, p50_ttfb_ms: 600, p90_latency_ms: 7200, p90_ttfb_ms: 1800, p95_latency_ms: 9500, p95_ttfb_ms: 2400, p99_latency_ms: 11800, p99_ttfb_ms: 2900},
    { model: "kimi-k3", requests: 30, ok_count: 28, input_tokens: 200, output_tokens: 600, credit: 4.5, credit_estimated: true, cost_usd: 0.4, cost_cny: 2.68, avg_latency_ms: 6100, avg_ttfb_ms: 1500, tps: 76.4, tps_visible: 40.2, tps_estimated: true, p50_latency_ms: 4100, p50_ttfb_ms: 1100, p90_latency_ms: 9600, p90_ttfb_ms: 3100, p95_latency_ms: 12115, p95_ttfb_ms: 3800, p99_latency_ms: 15400, p99_ttfb_ms: 4600},
  ],
};

export const USERS = {
  users: [
    { username: "alice", requests: 80, ok_count: 76, input_tokens: 700, output_tokens: 1500, cached_tokens: 300, credit: 9.75, credit_estimated: true, cost_usd: 1.2, cost_cny: 8.04, avg_latency_ms: 5600, avg_ttfb_ms: 1200, tps: 95.3, tps_visible: 55.0, tps_estimated: false, p50_latency_ms: 12800, p50_ttfb_ms: 3200, p90_latency_ms: 26400, p90_ttfb_ms: 6100, p95_latency_ms: 31871, p95_ttfb_ms: 7100, p99_latency_ms: 40200, p99_ttfb_ms: 8900},
    { username: "bob", requests: 40, ok_count: 38, input_tokens: 300, output_tokens: 500, cached_tokens: 100, credit: 2.75, credit_estimated: false, cost_usd: 0.64, cost_cny: 4.28, avg_latency_ms: 3900, avg_ttfb_ms: 800, tps: 110.0, tps_visible: 66.0, tps_estimated: true, p50_latency_ms: 5200, p50_ttfb_ms: 1100, p90_latency_ms: 9800, p90_ttfb_ms: 2200, p95_latency_ms: 12115, p95_ttfb_ms: 2600, p99_latency_ms: 15800, p99_ttfb_ms: 3300},
  ],
};

export const CREDENTIALS = {
  credentials: [
    { credential_id: "cred_abc123", credential_name: "主账号", provider: "trae",
      requests: 70, ok_count: 68, input_tokens: 600, output_tokens: 1200, cached_tokens: 250, credit: 5.25, credit_estimated: true, cost_usd: 0.5, cost_cny: 3.35, avg_latency_ms: 8200, avg_ttfb_ms: 2400, tps: 88.0, tps_visible: 61.0, tps_estimated: false, p50_latency_ms: 7759, p50_ttfb_ms: 3802, p90_latency_ms: 18844, p90_ttfb_ms: 9234, p95_latency_ms: 22170, p95_ttfb_ms: 10864, p99_latency_ms: 26604, p99_ttfb_ms: 13037},
    // 无昵称（老库/已删除）：回落 ID 前 12 位
    { credential_id: "cred_zzz999888777", credential_name: null, provider: "codebuddy",
      requests: 50, ok_count: 46, input_tokens: 400, output_tokens: 800, cached_tokens: 150, credit: 12.5, credit_estimated: false, cost_usd: 1.34, cost_cny: 8.99, avg_latency_ms: null, avg_ttfb_ms: null, tps: null, tps_visible: null, tps_estimated: false, p50_latency_ms: null, p50_ttfb_ms: null, p90_latency_ms: null, p90_ttfb_ms: null, p95_latency_ms: null, p95_ttfb_ms: null, p99_latency_ms: null, p99_ttfb_ms: null},
  ],
};

export const MODEL_TIMELINE = {
  models: ["glm-5.2", "kimi-k3"],
  points: [
    { hour: 1700000000, "glm-5.2": 4, "kimi-k3": 1 },
    { hour: 1700003600, "glm-5.2": 0, "kimi-k3": 2 },
  ],
};

/** 主图时间序列：渠道为动态键 + 后端算好的 `__total` 全渠道合计。 */
export const TIMELINE = {
  points: [
    { hour: 1700000000, __total: 3, trae: 2, codebuddy: 1 },
    { hour: 1700003600, __total: 7, trae: 4, codebuddy: 3 },
  ],
};

/** 延迟分位时序：P50 中位数 + P95 上界（读明细表，仅 90 天）。 */
export const PERCENTILE_TIMELINE = {
  metric: "latency",
  window_days: 90,
  points: [
    { hour: 1700000000, p50: 1200, p95: 5200, samples: 40 },
    { hour: 1700003600, p50: 2400, p95: 9800, samples: 12 },
  ],
};

export const EVENTS_EMPTY = { events: [], next_before: null };
export const BASE_TS = 1_700_000_000;

export const EVENTS_PAGE1 = {
  events: [
    { rowid: 3, ts: BASE_TS + 200, username: "root", provider: "codebuddy", model: "glm-5.2",
      credential_id: "cred_abc123", credential_name: "主账号",
      ok: 1, error_type: null, input_tokens: 120, output_tokens: 480, cached_tokens: 90,
      credit: 0.35, credit_estimated: 0, cost_usd: 0.0012, cost_cny: 0.008,
      latency_ms: 8200, ttfb_ms: 2400, request_id: "req_page1",
      // 后端算好的逐请求速度：正文 51.2 / 总产出 82.8 tok/s（同一生成窗口）
      tps: 82.8, tps_visible: 51.2 },
    { rowid: 2, ts: BASE_TS + 100, username: "root", provider: "trae", model: "deepseek-v4",
      credential_id: null, credential_name: null,
      ok: 0, error_type: "rate_limit", input_tokens: null, output_tokens: null, cached_tokens: null,
      credit: null, credit_estimated: 0, cost_usd: null, cost_cny: null,
      latency_ms: 460, ttfb_ms: 120, request_id: null,
      // 失败请求没有输出 → 速度不可测（前端显示 —）
      tps: null, tps_visible: null },
  ],
  next_before: 2,
};

export const EVENTS_PAGE2 = {
  events: [
    { rowid: 1, ts: BASE_TS, username: "root", provider: "trae", model: "kimi-k3",
      ok: 1, error_type: null, input_tokens: 5, output_tokens: 9, cached_tokens: null,
      credit: null, credit_estimated: 0, cost_usd: null, cost_cny: null,
      latency_ms: 300, ttfb_ms: 150, request_id: "req_page2",
      // 后端未给速度（本用例只关心分页，速度列在别处断言）
      tps: null, tps_visible: null },
  ],
  next_before: null,
};

/** 失败归因样例：失败数降序（后端口径），含未上报错误类型（NULL）。 */
export const ERRORS = {
  errors: [
    { provider: "trae", model: "glm-5.2", error_type: "rate_limit", failures: 12,
      rescued: 4, last_ts: BASE_TS + 300 },
    { provider: "codebuddy", model: "kimi-k3", error_type: "upstream_error", failures: 4,
      rescued: 4, last_ts: BASE_TS + 200 },
    { provider: "trae", model: "deepseek-v4", error_type: null, failures: 2,
      rescued: 0, last_ts: BASE_TS + 100 },
  ],
};
