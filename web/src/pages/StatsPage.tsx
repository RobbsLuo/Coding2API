import { useEffect, useState } from "react";
import { useSessionContext } from "../Layout";
import { useStatsByProvider, useStatsOverview } from "../api/hooks";
import { formatNumber } from "../api/display";
import type { Provider } from "../api/types";
import { Empty, Field, Input, Metric, Panel, Select } from "../ui";

const RANGES = [
  { value: "24h", label: "最近 24 小时", seconds: 86400 },
  { value: "7d", label: "最近 7 天", seconds: 7 * 86400 },
  { value: "30d", label: "最近 30 天", seconds: 30 * 86400 },
  { value: "all", label: "全部", seconds: 0 },
];

const PROVIDER_LABEL: Record<Provider, string> = { codebuddy: "CodeBuddy", trae: "TRAE" };

/** 用户名筛选的被控输入与服务端查询值分离，避免每敲一个字都打一次接口。 */
const FILTER_DEBOUNCE_MS = 300;

export function StatsPage() {
  const session = useSessionContext();
  const [range, setRange] = useState("7d");
  const [target, setTarget] = useState("");
  const [debouncedTarget, setDebouncedTarget] = useState("");

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedTarget(target), FILTER_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [target]);

  const seconds = RANGES.find((item) => item.value === range)?.seconds ?? 0;
  const since = seconds > 0 ? Math.floor(Date.now() / 1000) - seconds : undefined;
  const username = session.is_admin && debouncedTarget ? debouncedTarget : undefined;

  const overview = useStatsOverview(session.username, username, since);
  const byProvider = useStatsByProvider(session.username, username, since);

  if (overview.isLoading) return <Empty>载入中…</Empty>;
  const stats = overview.data;

  return (
    <div className="space-y-6" data-testid="stats-page">
      <Panel title="筛选">
        <div className="flex flex-wrap items-end gap-3">
          <div className="w-44">
            <Field label="时间范围">
              <Select
                value={range}
                data-testid="range-select"
                onChange={(event) => setRange(event.target.value)}
              >
                {RANGES.map((item) => (
                  <option key={item.value} value={item.value}>
                    {item.label}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
          {session.is_admin && (
            <div className="w-44">
              <Field label="用户名" hint="留空表示全部用户">
                <Input
                  value={target}
                  data-testid="username-filter"
                  placeholder="全部用户"
                  onChange={(event) => setTarget(event.target.value)}
                />
              </Field>
            </div>
          )}
        </div>
      </Panel>

      <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Metric label="请求数" value={formatNumber(stats?.requests)} />
        <Metric
          label="成功率"
          value={stats?.success_rate === null || stats?.success_rate === undefined
            ? "—"
            : `${(stats.success_rate * 100).toFixed(1)}%`}
        />
        <Metric label="输入 token" value={formatNumber(stats?.input_tokens)} />
        <Metric label="输出 token" value={formatNumber(stats?.output_tokens)} />
        <Metric label="推理 token" value={formatNumber(stats?.reasoning_tokens)} />
        <Metric
          label="平均延迟"
          value={stats?.avg_latency_ms === null || stats?.avg_latency_ms === undefined
            ? "—"
            : `${formatNumber(stats.avg_latency_ms)} ms`}
        />
        <Metric
          label="平均首字延迟"
          value={stats?.avg_ttfb_ms === null || stats?.avg_ttfb_ms === undefined
            ? "—"
            : `${formatNumber(stats.avg_ttfb_ms)} ms`}
        />
        <Metric
          label="Credit 消耗"
          value={stats?.credit === null || stats?.credit === undefined
            ? "—"
            : formatNumber(Number(stats.credit.toFixed(2)))}
          hint="上游可选字段，可能不返回"
        />
      </section>

      <Panel title="按上游分组">
        {(byProvider.data?.providers.length ?? 0) === 0 ? (
          <Empty data-testid="no-provider-stats">该范围内没有请求</Empty>
        ) : (
          <table className="w-full text-sm" data-testid="provider-table">
            <thead>
              <tr className="text-left text-xs text-[var(--color-ink-muted)]">
                <th className="pb-2 font-medium">上游</th>
                <th className="pb-2 font-medium">请求数</th>
                <th className="pb-2 font-medium">成功数</th>
                <th className="pb-2 font-medium">输入 token</th>
                <th className="pb-2 font-medium">输出 token</th>
                <th className="pb-2 font-medium">Credit</th>
              </tr>
            </thead>
            <tbody>
              {byProvider.data?.providers.map((row) => (
                <tr key={row.provider} className="border-t border-[var(--color-border-soft)]">
                  <td className="py-2">{PROVIDER_LABEL[row.provider] ?? row.provider}</td>
                  <td className="py-2 tabular-nums">{formatNumber(row.requests)}</td>
                  <td className="py-2 tabular-nums">{formatNumber(row.ok_count)}</td>
                  <td className="py-2 tabular-nums">{formatNumber(row.input_tokens)}</td>
                  <td className="py-2 tabular-nums">{formatNumber(row.output_tokens)}</td>
                  <td className="py-2 tabular-nums">
                    {row.credit === null ? "—" : formatNumber(Number(row.credit.toFixed(2)))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>

      <p className="text-xs text-[var(--color-ink-muted)]">
        统计不保存提示词、回答、请求头、Token、工具参数与原始错误体；逐请求明细保留 90 天，小时汇总永久保留。
      </p>
    </div>
  );
}
