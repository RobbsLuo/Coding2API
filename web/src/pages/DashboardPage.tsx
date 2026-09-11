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
import { Badge, Empty, Metric, Panel } from "../ui";

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

  if (isLoading) return <Empty>载入中…</Empty>;

  return (
    <div className="space-y-6" data-testid="dashboard">
      <section className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
        <Metric label="凭证总数" value={formatNumber(counts.total)} />
        <Metric label="可用" value={formatNumber(counts.ready)} />
        <Metric label="冷却中" value={formatNumber(counts.cooling)} />
        <Metric label="已禁用" value={formatNumber(counts.disabled)} />
        <Metric label="额度耗尽" value={formatNumber(counts.exhausted)} />
        <Metric label="已关闭" value={formatNumber(counts.off)} />
      </section>

      <Panel title="健康度三态分布">
        <div className="flex flex-wrap gap-3 text-sm">
          <span data-testid="health-known">
            已知剩余：<strong>{healthTally.known}</strong>
          </span>
          <span data-testid="health-unknown" className="text-[var(--color-ink-muted)]">
            未探测到额度：<strong>{healthTally.unknown}</strong>
          </span>
          <span data-testid="health-exhausted" className="text-[var(--color-danger)]">
            已耗尽：<strong>{healthTally.exhausted}</strong>
          </span>
        </div>
        <p className="mt-2 text-xs text-[var(--color-ink-muted)]">
          「未探测到额度」表示探测失败或上游未提供额度信息，与「已耗尽」含义不同，不应互相替代。
        </p>
      </Panel>

      <Panel title="按时段上游分组">
        <div className="grid gap-4 md:grid-cols-2">
          {byProvider.map(({ provider, items }) => (
            <div key={provider} data-testid={`provider-group-${provider}`}>
              <div className="mb-2 text-xs font-medium text-[var(--color-ink-muted)]">
                {PROVIDER_LABEL[provider]}（{items.length}）
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
                <span>
                  {item.nickname || item.id.slice(0, 12)}
                  <span className="ml-2 text-xs text-[var(--color-ink-muted)]">
                    {PROVIDER_LABEL[item.provider]}
                  </span>
                </span>
                <span className="text-xs text-[var(--color-warn)]">
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

function CredentialRow({ credential, now }: { credential: Credential; now: number }) {
  const health = healthView(credential.health);
  const state = credentialState(credential, now);
  return (
    <li className="flex items-center justify-between gap-3 rounded-lg border border-[var(--color-border-soft)] px-3 py-2 text-sm">
      <span className="truncate">{credential.nickname || credential.id.slice(0, 12)}</span>
      <span className="flex shrink-0 items-center gap-2">
        <Badge tone={health.tone}>{health.label}</Badge>
        <Badge tone={STATE_TONE[state]}>{STATE_LABEL[state]}</Badge>
      </span>
      <span className="shrink-0 text-xs text-[var(--color-ink-muted)]">
        {formatNumber(credential.quota_remaining)}/{formatNumber(credential.quota_total)}
        <span className="ml-1">{quotaSemantics(credential)}</span>
      </span>
    </li>
  );
}
