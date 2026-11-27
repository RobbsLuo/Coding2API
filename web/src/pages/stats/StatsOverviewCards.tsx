/**
 * 概览指标：一块面板、两排读数（自 `pages/StatsPage.tsx` 原样搬出，随后合并）。
 *
 * 之前是三块独立的框（四张主卡 + 次要指标条 + 压缩收益面板），三排下来占掉
 * 首屏 350px，却只有 11 个数字。现在合成**一块**带发丝分隔线的表面：
 * - 第一排 4 个主指标（请求 / token / 成本 / 耗时），字号最大；
 * - 第二排 7 个辅指标（速度 / 缓存 / Credit + 压缩 4 项），同一档小字号；
 * - 底部一行脚注放判定口径与压缩开关状态。
 * 指标数量不变（4 + 7），层级只靠字号与位置，不再靠三套边框/底色互相竞争。
 *
 * 派生文案（成功率 / token 提示 / 缓存命中与未命中）就在本组件算，
 * 与搬出前同址同式，不跨组件传中间值。
 */
import { ToggleLeft, ToggleRight } from "lucide-react";
import { Metric, Notice } from "../../ui";
import {
  formatAssumedCacheRate,
  formatCompact,
  formatCredit,
  formatLatency,
  formatMoney,
  formatNumber,
  formatRate,
  formatSpeed,
} from "../../api/display";
import type { StatsOverview } from "../../api/types";
import { useCompressionFeed } from "./compression";

interface StatsOverviewCardsProps {
  overviewFailed: boolean;
  stats: StatsOverview | undefined;
  /** 压缩读数是池级计数，只有 operator+ 可见。 */
  isOperator: boolean;
  /** 当前时间窗（秒），与总览同源，保证「同期」。 */
  since?: number;
  until?: number;
  /** 自定义范围非法时暂停取数。 */
  rangeReady?: boolean;
}

/** 一行读数：标签 + 小号数值 + 可选说明。用于第二排辅指标。 */
function StatCell({
  label,
  value,
  hint,
  testId,
  className,
}: {
  label: string;
  value: React.ReactNode;
  hint?: React.ReactNode;
  testId?: string;
  className?: string;
}) {
  return (
    <div className={`bg-panel px-3.5 py-3 ${className ?? ""}`} data-testid={testId}>
      <div className="text-xs font-medium text-muted-foreground">{label}</div>
      <div className="mt-1 text-base leading-tight font-semibold tabular-nums">{value}</div>
      {hint && (
        <div className="mt-0.5 text-xs leading-snug text-muted-foreground">{hint}</div>
      )}
    </div>
  );
}

export function StatsOverviewCards({
  overviewFailed,
  stats,
  isOperator,
  since,
  until,
  rangeReady = true,
}: StatsOverviewCardsProps) {
  const rate =
    stats?.success_rate === null || stats?.success_rate === undefined
      ? null
      : stats.success_rate;
  const rateText = rate === null ? "—" : `${(rate * 100).toFixed(1)}%`;
  const totalTokens = (stats?.input_tokens ?? 0) + (stats?.output_tokens ?? 0);
  // 输入缓存：命中 = 上报的 cached_tokens；未命中 = 已上报请求的输入 − 命中。
  // null（上游未上报，如 TRAE）= 没有数据，不是「0 命中」。后端给了该渠道的
  // 校准命中率（cache_rate_assumed）时，展示 ≈ 推算值，明确不是上游回传。
  const cached = stats?.cached_tokens ?? null;
  const cachedInput = stats?.cached_input_tokens ?? null;
  const cacheReported = cached !== null;
  const assumed = cacheReported ? null : (stats?.cache_rate_assumed ?? null);
  const hitText = cached !== null
    ? formatCompact(cached)
    : assumed !== null && stats?.input_tokens != null
      ? `≈${formatCompact(Math.round(stats.input_tokens * assumed))}`
      : "—";
  const missText = cached !== null
    ? (cachedInput === null ? "—" : formatCompact(cachedInput - cached))
    : assumed !== null && stats?.input_tokens != null
      ? `≈${formatCompact(Math.round(stats.input_tokens * (1 - assumed)))}`
      : "—";
  // 命中率用后端算好的 cache_rate（分母只含已上报请求）；未上报时用校准值加 ≈。
  const cacheRateText = cacheReported
    ? formatRate(stats?.cache_rate)
    : formatAssumedCacheRate(assumed);
  // 部分渠道不上报缓存字段：命中率只覆盖已上报的那部分请求，得说清楚。
  const partialCache =
    cachedInput !== null
    && stats?.input_tokens !== null
    && stats?.input_tokens !== undefined
    && cachedInput < stats.input_tokens;
  const tokenHint =
    `输入 ${formatCompact(stats?.input_tokens)}（命中 ${hitText} · 未命中 ${missText}）`
    + ` · 输出 ${formatCompact(stats?.output_tokens)} · 推理 ${formatCompact(stats?.reasoning_tokens)}`
    + ` · 缓存命中率 ${cacheRateText}`
    + (cacheReported && partialCache ? "（按已上报请求计算）" : "");
  const cacheNotice = stats === undefined
    ? null
    : cacheReported
      ? (partialCache ? "命中率按已上报请求计算" : null)
      : assumed !== null
        ? "上游未上报缓存字段，命中数与命中率为按校准比例的推算（≈）"
        : "上游未上报缓存字段，显示 — 不代表未命中";

  const compression = useCompressionFeed({ stats, isOperator, since, until, rangeReady });
  const counters = compression.counters;

  return (
    <>
      {overviewFailed && (
        <div data-testid="stats-overview-error">
          <Notice tone="danger">
            总览数据加载失败：服务可能暂时不可用或会话已过期。
            下方指标显示为「—」是取不到数，不是真的没有用量。
          </Notice>
        </div>
      )}

      {/* 一块表面、发丝分隔线：`gap-px` 让相邻格子自己画线，不再给每格套边框。
          主指标不带 icon、不带 tone：请求量不是「成功」语义，绿色留给真正的
          成功状态；装饰性图标不传达信息，label 已说明一切。 */}
      <section
        aria-label="用量概览统计"
        className="flex flex-col gap-px overflow-hidden rounded-xl border border-border/70 bg-border/70 shadow-sm"
      >
        <div className="grid grid-cols-2 gap-px bg-border/70 lg:grid-cols-4">
          <Metric
            label="请求数"
            value={formatNumber(stats?.requests)}
            hint={`成功 ${formatNumber(stats?.ok_count)} · 成功率 ${rateText}`}
            className="rounded-none bg-panel shadow-none ring-0"
          />
          <Metric
            label="Token 消耗"
            value={formatCompact(totalTokens)}
            hint={tokenHint}
            className="rounded-none bg-panel shadow-none ring-0"
          />
          <Metric
            label="成本（估算）"
            value={formatMoney(stats?.cost_cny, "CNY")}
            hint={`美元 ${formatMoney(stats?.cost_usd, "USD")} · 按 OpenRouter 刊例价折算`}
            className="rounded-none bg-panel shadow-none ring-0"
          />
          <Metric
            label="平均耗时"
            value={formatLatency(stats?.avg_latency_ms)}
            hint={`首字延迟 ${formatLatency(stats?.avg_ttfb_ms)}`}
            className="rounded-none bg-panel shadow-none ring-0"
          />
        </div>

        {/* 第二排：7 个辅指标同档。窄屏 2 列（Credit 占满一行、压缩 4 项成 2×2），
            xl 起收成一行 7 格；两种宽度都填满，不留 border 色的空格。 */}
        <div className="grid grid-cols-2 gap-px bg-border/70 xl:grid-cols-7">
          <StatCell
            label="输出速度"
            testId="stat-speed"
            value={formatSpeed(stats?.tps_visible, stats?.tps_estimated)}
            hint={
              stats?.tps === null
                ? "该范围内没有可用的生成时间样本"
                : `总产出（含思考）${formatSpeed(stats?.tps, stats?.tps_estimated)}`
            }
          />
          <StatCell
            label="缓存命中率"
            testId="stat-cache"
            value={cacheRateText}
            hint={
              <>
                命中 {hitText} · 未命中 {missText}
                {cacheNotice && (
                  <span className="mt-0.5 block" data-testid="stat-cache-note">
                    {cacheNotice}
                  </span>
                )}
              </>
            }
          />
          <StatCell
            label="Credit 消耗"
            testId="stat-credit"
            value={formatCredit(stats?.credit, stats?.credit_estimated)}
            hint="渠道可选字段；≈ 为推算值"
            className="col-span-2 xl:col-span-1"
          />

          {isOperator && (
            <div
              className="contents"
              data-testid="compress-panel"
            >
              <StatCell
                label="压缩触发次数"
                value={formatNumber(counters?.triggers)}
                hint={`tool 输出 ${formatNumber(counters?.tool_triggers)} · 上下文裁剪 ${formatNumber(counters?.context_trims)}`}
              />
              <StatCell
                label="省下 token（估算）"
                value={formatCompact(counters?.tokens_saved)}
                hint={
                  <>
                    字符 {formatCompact(counters?.chars_saved)} · 按 0.25 token/字符下界估算
                  </>
                }
              />
              <StatCell
                label="同期缓存命中率"
                value={compression.hitRate}
                hint={`命中 ${formatCompact(stats?.cached_tokens ?? null)} · 输入 ${formatCompact(stats?.input_tokens)}`}
              />
              <StatCell
                label="原地不变"
                value={formatNumber(counters?.unchanged)}
                hint="开关开着但载荷没变 = 白开证据"
              />
            </div>
          )}
        </div>

        {isOperator && (
          <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 bg-panel px-4 py-2.5 text-xs text-muted-foreground">
            <span className="inline-flex items-center gap-1.5">
              {compression.data?.tool_output_compress_enabled
                ? <ToggleRight className="size-4 text-ok" aria-hidden />
                : <ToggleLeft className="size-4" aria-hidden />}
              {compression.switchLabel}
            </span>
            {compression.loading && (
              <span data-testid="compress-panel-loading">正在读取压缩计数…</span>
            )}
            {compression.failed && (
              <span data-testid="compress-panel-error">
                压缩读数不可用（端点无响应或会话已过期）；这不影响压缩本身，按开关继续生效。
              </span>
            )}
            {compression.idle && (
              <span data-testid="compress-panel-empty">
                所选时间窗内压缩未触发，也没有可压的 tool 输出或裁掉的历史；上方「缓存命中率」即为当前代价。
              </span>
            )}
            <span>
              判断口径：省下的 token 要大于命中率下降造成的未命中 token 才值得开；
              命中率异常时先看「上下文裁剪」次数。计数按小时分桶、保留 90 天，「全部」为终身累计。
            </span>
          </div>
        )}
      </section>
    </>
  );
}