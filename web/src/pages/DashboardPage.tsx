import { useCredentials } from "../api/hooks";
import {
  credentialState,
  cooldownRemaining,
  formatDuration,
  formatNumber,
  healthView,
  quotaSemantics,
  STATE_LABEL,
  STATE_TONE,
} from "../api/display";
import type { Credential } from "../api/types";
import { Badge, Empty, Metric, Panel, Skeleton } from "../ui";
import { PageHeader } from "../components/PageHeader";
import { ProviderIcon } from "../components/ProviderIcon";
import { providerLabel } from "../api/providers";
import { cn } from "@/lib/utils";
import { Ban, BatteryLow, CheckCircle2, Database, LayoutDashboard, Timer, ToggleLeft } from "lucide-react";

/** 冷却倒计时随时间流逝，30s 轮询足以让「剩余 59 分钟」这类文案保持可信。 */
const DASHBOARD_REFETCH_MS = 30_000;

export function DashboardPage() {
  // refetchInterval：冷却倒计时/健康度是「随时间变化」的观测量，页面停留时
  // 应自动刷新（与 useTasks 的 30s 同口径）。
  const { data, isLoading } = useCredentials(undefined, DASHBOARD_REFETCH_MS);

  const credentials = data?.credentials ?? [];
  const now = Date.now() / 1000;

  const counts = {
    total: credentials.length,
    ready: 0,
    cooling: 0,
    disabled: 0,
    off: 0,
    exhausted: 0,
  };
  const healthTally = { known: 0, unknown: 0, noprobe: 0, exhausted: 0 };

  for (const credential of credentials) {
    counts[credentialState(credential, now)] += 1;
    healthTally[healthView(credential.health, credential.provider).kind] += 1;
  }

  const cooling = credentials.filter((item) => credentialState(item, now) === "cooling");
  if (isLoading) {
    return (
      <div className="space-y-6" data-testid="dashboard">
        <p className="sr-only">载入中…</p>
        <SkeletonMetricGrid />
        <Panel title="凭证池">
          <div className="space-y-2">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
          </div>
        </Panel>
      </div>
    );
  }

  return (
    <div className="space-y-6" data-testid="dashboard">
      <PageHeader
        title="池仪表盘"
        description="凭证池整体健康与配额概况：健康度按剩余积分占比四态统计（已知 / 未探测 / 无探测 / 已耗尽），切换标签页可对凭证进行维护。"
        icon={<LayoutDashboard className="size-5" />}
      />

      <section
        aria-label="凭证状态统计"
        className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6"
      >
        <Metric label="凭证总数" value={formatNumber(counts.total)} icon={<Database className="size-4" />} />
        <Metric label="可用" value={formatNumber(counts.ready)} tone="ok" icon={<CheckCircle2 className="size-4" />} />
        <Metric label="冷却中" value={formatNumber(counts.cooling)} tone="warn" icon={<Timer className="size-4" />} />
        <Metric label="已禁用" value={formatNumber(counts.disabled)} icon={<Ban className="size-4" />} />
        <Metric label="额度耗尽" value={formatNumber(counts.exhausted)} tone="danger" icon={<BatteryLow className="size-4" />} />
        <Metric label="已暂停" value={formatNumber(counts.off)} icon={<ToggleLeft className="size-4" />} />
      </section>

      <Panel title="健康度四态分布">
        {/* 纯色块拼条对读屏不可读：补一个汇总文本语义（图例已承载具体数值） */}
        <div
          role="img"
          aria-label={`健康度分布：已知剩余 ${healthTally.known}，未探测 ${healthTally.unknown}，无探测 ${healthTally.noprobe}，已耗尽 ${healthTally.exhausted}`}
          className="flex h-2 w-full overflow-hidden rounded-full bg-muted"
        >
          {healthTally.known > 0 && (
            <div
              className="bg-ok transition-all"
              style={{ width: `${(healthTally.known / Math.max(1, credentials.length)) * 100}%` }}
            />
          )}
          {healthTally.unknown > 0 && (
            <div
              className="bg-warn transition-all"
              style={{ width: `${(healthTally.unknown / Math.max(1, credentials.length)) * 100}%` }}
            />
          )}
          {healthTally.noprobe > 0 && (
            <div
              className="bg-muted-foreground/40 transition-all"
              style={{ width: `${(healthTally.noprobe / Math.max(1, credentials.length)) * 100}%` }}
            />
          )}
          {healthTally.exhausted > 0 && (
            <div
              className="bg-destructive transition-all"
              style={{ width: `${(healthTally.exhausted / Math.max(1, credentials.length)) * 100}%` }}
            />
          )}
        </div>
        <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1.5 text-sm">
          <span data-testid="health-known" className="inline-flex items-center gap-1.5">
            <span className="size-2 rounded-full bg-ok" />
            已知剩余：<strong>{healthTally.known}</strong>
          </span>
          <span data-testid="health-unknown" className="inline-flex items-center gap-1.5 text-muted-foreground">
            <span className="size-2 rounded-full bg-warn" />
            未探测：<strong>{healthTally.unknown}</strong>
          </span>
          <span data-testid="health-noprobe" className="inline-flex items-center gap-1.5 text-muted-foreground">
            <span className="size-2 rounded-full bg-muted-foreground/40" />
            无探测：<strong>{healthTally.noprobe}</strong>
          </span>
          <span data-testid="health-exhausted" className="inline-flex items-center gap-1.5 text-destructive">
            <span className="size-2 rounded-full bg-destructive" />
            已耗尽：<strong>{healthTally.exhausted}</strong>
          </span>
        </div>
        <p className="mt-2 text-xs text-muted-foreground">
          「未探测」表示探测失败或渠道未提供额度信息，与「已耗尽」含义不同，不应互相替代；
          「无探测」是 OpenCode Zen / Kilo Gateway 免费层——上游没有额度接口，探也没用。
        </p>
      </Panel>

      <Panel title="凭证池">
        {credentials.length === 0 ? (
          <Empty data-testid="no-credentials">暂无凭证</Empty>
        ) : (
          <ul className="grid grid-cols-1 gap-2.5 md:grid-cols-2 xl:grid-cols-3" data-testid="credential-list">
            {credentials.map((item) => (
              <CredentialRow key={item.id} credential={item} now={now} />
            ))}
          </ul>
        )}
      </Panel>

      <Panel title="冷却中的账号">
        {cooling.length === 0 ? (
          <Empty data-testid="no-cooling">当前没有冷却中的凭证</Empty>
        ) : (
          <ul className="space-y-1.5" data-testid="cooling-list">
            {cooling.map((item) => (
              <li key={item.id} className="flex items-center justify-between text-sm">
                <span className="flex items-center gap-2">
                  <span className="size-1.5 rounded-full bg-warn" />
                  {item.nickname || item.id.slice(0, 12)}
                  <span className="text-xs text-muted-foreground">
                    {providerLabel(item.provider)}
                  </span>
                </span>
                <span className="text-xs font-medium text-warn">
                  剩余 {formatDuration(cooldownRemaining(item.cooling_until, now))}
                  {item.disabled_reason ? ` · ${item.disabled_reason}` : ""}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}

function SkeletonMetricGrid() {
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
      {Array.from({ length: 6 }).map((_, index) => (
        <div key={index} className="rounded-xl px-4 py-3 ring-1 ring-border">
          <Skeleton className="h-3 w-14" />
          <Skeleton className="mt-2 h-6 w-10" />
        </div>
      ))}
    </div>
  );
}

function CredentialRow({ credential, now }: { credential: Credential; now: number }) {
  const health = healthView(credential.health, credential.provider);
  const state = credentialState(credential, now);
  // 配额使用率（已用 %）：total>0 时才有意义，否则不展示
  const quotaTotal = credential.quota_total ?? 0;
  const usedPct =
    quotaTotal > 0 && credential.quota_remaining !== null
      ? Math.max(0, Math.min(100, Math.round((1 - credential.quota_remaining / quotaTotal) * 100)))
      : null;
  const barTone =
    health.percent === null
      ? ""
      : health.percent >= 50
        ? "bg-ok"
        : health.percent > 0
          ? "bg-warn"
          : "bg-destructive";
  const noprobe = health.kind === "noprobe";
  return (
    <li
      className="rounded-xl bg-card p-3.5 ring-1 ring-border transition-colors hover:ring-primary/30"
      data-testid={`credential-${credential.id}`}
      data-provider={credential.provider}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="flex min-w-0 items-center gap-1.5">
          <ProviderIcon provider={credential.provider} size={14} />
          <span className="shrink-0 text-xs text-muted-foreground">
            {providerLabel(credential.provider)}
          </span>
          <span className="truncate font-medium">
            {credential.nickname || credential.id.slice(0, 12)}
          </span>
        </span>
        <span className="flex shrink-0 items-center gap-1.5">
          <Badge tone={health.tone}>{health.label}</Badge>
          <Badge tone={STATE_TONE[state]}>{STATE_LABEL[state]}</Badge>
        </span>
      </div>
      <div className="mt-3">
        <div className="flex items-end justify-between gap-2">
          <span className="text-lg font-bold tabular-nums">
            {health.percent !== null ? `${health.percent}%` : "—"}
          </span>
          <span className="text-xs text-muted-foreground tabular-nums">
            {formatNumber(credential.quota_remaining)}/{formatNumber(credential.quota_total)}
            <span className="ml-1">{quotaSemantics(credential)}</span>
            {usedPct !== null && (
              <span className="ml-1.5 text-muted-foreground/80">已用 {usedPct}%</span>
            )}
          </span>
        </div>
        <div className="mt-1.5 flex items-center gap-1.5">
          <div
            className={cn(
              "h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-muted",
              // 无探测（免费层）时整条轨道降透明度，读作「该卡没有额度条」
              // 而不是「额度为零」。
              noprobe && "opacity-50",
            )}
          >
            {health.percent !== null && (
              <div
                className={cn("h-full rounded-full transition-all", barTone)}
                style={{ width: `${health.percent}%` }}
              />
            )}
          </div>
        </div>
      </div>
    </li>
  );
}