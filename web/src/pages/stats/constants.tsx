/**
 * 统计页专有常量与类型（自 `pages/StatsPage.tsx` 顶部原样搬出）。
 *
 * 只服务本页子树：列定义与维度表留在这里，不往 `components/` 提炼，
 * 避免把「统计页口径」升级成跨页共享抽象。
 */
import { Activity, BarChart3, Coins, Gauge, Timer, Wallet } from "lucide-react";
import type { GroupStats, StatsDrill } from "../../api/types";
import type { SortOrder } from "../../hooks/useSort";

/** 实时流前端缓冲上限：只保留最近 50 条（新→旧），页面长开也不会无限增长。 */
export const LIVE_MAX_ROWS = 50;

export const RANGES = [
  { value: "24h", label: "最近 24 小时", seconds: 86400 },
  { value: "7d", label: "最近 7 天", seconds: 7 * 86400 },
  { value: "30d", label: "最近 30 天", seconds: 30 * 86400 },
  { value: "all", label: "全部", seconds: 0 },
  // 自定义：两个 datetime-local 输入，起止任意。以「本地时区、精确到分钟」为准。
  { value: "custom", label: "自定义", seconds: -1 },
];

/**
 * 图表指标：按「要回答什么问题」分组。
 *
 * 请求次数 / Token 回答「用量」；成本 / 单请求成本回答「花得值不值」；
 * 耗时 / 首字回答「快不快」；成功率 / 缓存命中率回答「稳不稳、省不省」。
 * 八个指标共用一张主图（切换而非叠加），渠道拆分由主图下方的渠道切换控制。
 */
export const METRICS = [
  { value: "requests", label: "请求次数", icon: <BarChart3 className="size-3.5" /> },
  { value: "tokens", label: "Token", icon: <Coins className="size-3.5" /> },
  { value: "cost", label: "成本", icon: <Wallet className="size-3.5" /> },
  { value: "cost_per_request", label: "单请求成本", icon: <Wallet className="size-3.5" /> },
  { value: "latency", label: "耗时", icon: <Timer className="size-3.5" /> },
  { value: "ttfb", label: "首字延迟", icon: <Gauge className="size-3.5" /> },
  { value: "success_rate", label: "成功率", icon: <Activity className="size-3.5" /> },
  { value: "cache_rate", label: "缓存命中率", icon: <Coins className="size-3.5" /> },
] as const;

/** 延迟类指标：主图之外再画一张分位图（P50 中位数 + P95 上界）。 */
export const LATENCY_METRICS = new Set(["latency", "ttfb"]);

/**
 * 请求明细的合并列：把同类列并成一列，默认值一眼可读，悬停展开全部子项。
 *
 * Token = 输入 / 输出 / 命中 / 命中率；延迟 = 耗时 / 首字。合并把列数从 14 压到
 * 约 10（窄屏卡片也少一半字段），排序能力靠列头下拉选子列保留（见 SortMenuHead）。
 */
export const TOKEN_COLUMNS = [
  { key: "input_tokens", label: "输入" },
  { key: "output_tokens", label: "输出" },
  { key: "cached_tokens", label: "命中" },
  { key: "cache_rate", label: "命中率" },
] as const;

export const LATENCY_COLUMNS = [
  { key: "latency_ms", label: "耗时" },
  { key: "ttfb_ms", label: "首字" },
] as const;

/**
 * 明细「渠道 / 凭证」合并列的子列：渠道 / 凭证。
 *
 * 单元格主行给凭证、副行给渠道（同渠道常挂多个账号，先认账号）；排序能力靠列头
 * 下拉保留（两个子键都还在），故列头同时写明两列。
 */
export const SOURCE_COLUMNS = [
  { key: "provider", label: "渠道" },
  { key: "credential", label: "凭证" },
] as const;

/**
 * 分组表请求合并列的子列：请求数 / 成功数（成功率是派生只读值，不可排序）。
 * 与 Token 列同例：默认只显示请求数，悬停补全成功数与成功率。
 */
export const GROUP_REQUEST_COLUMNS = [
  { key: "requests", label: "请求数" },
  { key: "ok_count", label: "成功数" },
] as const;

/**
 * 分组表 Token 合并列的子列：输入 / 输出 / 命中（+命中率，派生列不可排序）。
 * 只取明细表同名集合的前三项——`cache_rate` 后端没有分组排序键。
 */
export const GROUP_TOKEN_COLUMNS = [
  { key: "input_tokens", label: "输入" },
  { key: "output_tokens", label: "输出" },
  { key: "cached_tokens", label: "命中" },
] as const;

/** 分组表两种延迟口径：均值读小时汇总，分位读明细（菜单即排序键白名单）。 */
export const GROUP_AVG_LATENCY_COLUMNS = [
  { key: "avg_latency_ms", label: "平均耗时" },
  { key: "avg_ttfb_ms", label: "平均首字" },
] as const;

/**
 * 分组表分位档：后端一次算好 P50/P90/P95/P99，前端只切展示哪一档。
 *
 * 四档全铺成列会把表头撑爆，故合并成一列 + 档位切换；默认 P95 保持既有
 * 观感与 testId（`group-p95-latency` / `group-sort-p95-latency`）。
 */
export const PERCENTILES = ["p50", "p90", "p95", "p99"] as const;
export type Percentile = (typeof PERCENTILES)[number];

export const PERCENTILE_TABS = PERCENTILES.map((value) => ({
  value, label: value.toUpperCase(),
}));

/**
 * 分组统计的维度。后端各维度的行结构一致（分组键 + 同一组公共指标），
 * 前端按 key 取出分组键列即可，表格本身不分叉。
 *
 * 用户/凭证维度标记 operatorOnly：viewer 只能看自己，恒是「我」一行，
 * 给这个 tab 只是噪音。
 */
export const GROUP_DIMENSIONS = [
  { value: "provider", label: "按渠道", groupKey: "provider", label2: "渠道", operatorOnly: false },
  { value: "model", label: "按模型", groupKey: "model", label2: "模型", operatorOnly: false },
  { value: "user", label: "按用户", groupKey: "username", label2: "用户", operatorOnly: true },
  { value: "credential", label: "按凭证", groupKey: "credential_id", label2: "凭证", operatorOnly: true },
] as const;

/** 分位列名（后端按 PERCENTILES 下发；types.ts 的 GroupStats 只写死了 P95）。 */
type PercentileField = `${Percentile}_latency_ms` | `${Percentile}_ttfb_ms`;

/** 公共指标 + 分位列 + 至多一个分组键（键名随维度变化，用可选字段统一承载）。 */
export type GroupRow = GroupStats & Partial<Record<PercentileField, number | null>> & {
  provider?: string;
  model?: string;
  username?: string;
  credential_id?: string;
  credential_name?: string | null;
};

/**
 * 页面级钻取状态 = 既有 `StatsDrill` + 成功/失败过滤。
 *
 * `ok` 不进 `types.ts` 的 `StatsDrill`（那个类型描述的是「分组表点行」的通用
 * 形态）：只有失败归因卡片需要它。这里本地扩展，`applyDrill` 合并时一并带上。
 */
export type PageDrill = StatsDrill & { ok?: number };

/**
 * 速度合并列的子列（分组表与明细表共用同一组排序键）：正文 / 总产出。
 *
 * 两列同分母（生成时间总和），分子不同——正文贴合屏幕刷新速度，总产出含思考
 * token（思考成批快速吐出，数字明显更高），故并成一列 + 下拉选排序键。
 */
export const SPEED_COLUMNS = [
  { key: "tps_visible", label: "正文速度" },
  { key: "tps", label: "总产出速度" },
] as const;

/** 分组维度描述（`GROUP_DIMENSIONS` 的元素）。 */
export type GroupDimension = (typeof GROUP_DIMENSIONS)[number];

/**
 * 后端排序状态（`useSort` 的返回值形态）：子组件只读列名 / 方向并透传点击，
 * 排序状态本身仍由页面持有，避免子组件各自维护一份。
 */
export interface StatsSortState {
  sort: string;
  order: SortOrder;
  toggle: (columnKey: string) => void;
}
