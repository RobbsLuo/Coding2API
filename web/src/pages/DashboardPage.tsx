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
import type { Credential, Provider } from "../api/types";
import { Badge, Empty, Metric, Panel, Skeleton } from "../ui";
import { PageHeader } from "../components/PageHeader";
import { cn } from "@/lib/utils";

const PROVIDER_LABEL: Record<Provider, string> = { codebuddy: "CodeBuddy", trae: "TRAE" };

export function DashboardPage() {
  const { data, isLoading } = useCredentials();
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
  const healthTally = { known: 0, unknown: 0, exhausted: 0 };

  for (const credential of credentials) {
    counts[credentialState(credential, now)] += 1;
    healthTally[healthView(credential.health).kind] += 1;
  }

  const cooling = credentials.filter((item) => credentialState(item, now) === "cooling");
  const byProvider = (["codebuddy", "trae"] as Provider[]).map((provider) => ({
    provider,
    items: credentials.filter((item) => item.provider === provider),
  }));

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
        description="凭证池整体健康与配额概况：健康度按剩余积分占比三态统计，切换标签页可对凭证进行维护。"
      />

      <section className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
        <Metric label="凭证总数" value={formatNumber(counts.total)} />
        <Metric label="可用" value={formatNumber(counts.ready)} tone="ok" />
        <Metric label="冷却中" value={formatNumber(counts.cooling)} tone="warn" />
        <Metric label="已禁用" value={formatNumber(counts.disabled)} />
        <Metric label="额度耗尽" value={formatNumber(counts.exhausted)} tone="danger" />
        <Metric label="已关闭" value={formatNumber(counts.off)} />
      </section>

      <Panel title="健康度三态分布">
        <div className="flex h-2 w-full overflow-hidden rounded-full bg-muted">
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
            未探测到额度：<strong>{healthTally.unknown}</strong>
          </span>
          <span data-testid="health-exhausted" className="inline-flex items-center gap-1.5 text-destructive">
            <span className="size-2 rounded-full bg-destructive" />
            已耗尽：<strong>{healthTally.exhausted}</strong>
          </span>
        </div>
        <p className="mt-2 text-xs text-muted-foreground">
          「未探测到额度」表示探测失败或上游未提供额度信息，与「已耗尽」含义不同，不应互相替代。
        </p>
      </Panel>

      <Panel title="按时段上游分组">
        <div className="grid gap-4 md:grid-cols-2">
          {byProvider.map(({ provider, items }) => (
            <div key={provider} data-testid={`provider-group-${provider}`}>
              <div className="mb-2 flex items-center text-xs font-medium text-muted-foreground">
                {PROVIDER_LABEL[provider]}（
                <span className="rounded-full bg-muted px-1.5 py-0.5 tabular-nums">
                  {items.length}
                </span>
                ）
              </div>
              {items.length === 0 ? (
                <Empty>暂无凭证</Empty>
              ) : (
                <ul className="space-y-1.5">
                  {items.map((item) => (
                    <CredentialRow key={item.id} credential={item} now={now} />
                  ))}
                </ul>
              )}
            </div>
          ))}
        </div>
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
                    {PROVIDER_LABEL[item.provider]}
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
        <div key={index} className="rounded-xl border border-border p-4">
          <Skeleton className="h-3 w-14" />
          <Skeleton className="mt-2 h-6 w-10" />
        </div>
      ))}
    </div>
  );
}

function CredentialRow({ credential, now }: { credential: Credential; now: number }) {
  const health = healthView(credential.health);
  const state = credentialState(credential, now);
  const barTone =
    health.percent === null
      ? ""
      : health.percent >= 50
        ? "bg-ok"
        : health.percent > 0
          ? "bg-warn"
          : "bg-destructive";
  return (
    <li className="rounded-lg border border-border bg-card px-3 py-2.5 text-sm transition-colors hover:bg-muted/40">
      <div className="flex items-center justify-between gap-3">
        <span className="truncate font-medium">
          {credential.nickname || credential.id.slice(0, 12)}
        </span>
        <span className="flex shrink-0 items-center gap-2">
          <Badge tone={health.tone}>{health.label}</Badge>
          <Badge tone={STATE_TONE[state]}>{STATE_LABEL[state]}</Badge>
        </span>
      </div>
      <div className="mt-2 flex items-center gap-3">
        {health.percent !== null ? (
          <div className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-muted">
            <div
              className={cn("h-full rounded-full transition-all", barTone)}
              style={{ width: `${health.percent}%` }}
            />
          </div>
        ) : (
          <span className="h-1.5 flex-1" />
        )}
        <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
          {formatNumber(credential.quota_remaining)}/{formatNumber(credential.quota_total)}
          <span className="ml-1">{quotaSemantics(credential)}</span>
        </span>
      </div>
    </li>
  );
}