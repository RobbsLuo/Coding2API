import {
  Ban,
  BatteryLow,
  CheckCircle2,
  Database,
  Timer,
  ToggleLeft,
} from "lucide-react";
import { formatNumber, healthView, credentialState } from "../api/display";
import type { Credential } from "../api/types";
import { Metric } from "../ui";

/**
 * 池概览：状态统计卡 + 健康度四态分布条（原「池仪表盘」页的独有内容）。
 *
 * 合并进凭证页顶部（原仪表盘的凭证池卡片列表、冷却列表与凭证表格重复，
 * 已随页面一并移除）；`now` 由调用方传入（凭证页已有 `Date.now()/1000`）。
 */
export function PoolOverview({ credentials, now }: {
  credentials: Credential[];
  now: number;
}) {
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

  return (
    <section
      className="rounded-xl bg-card px-4 py-4 ring-1 ring-border"
      data-testid="pool-overview"
    >
      <div className="mb-3 text-sm font-medium">池概况</div>
      {/* 6 个状态统计块并进同一容器：去掉各自的 Card 边框/底色，只留分隔线，
          整块读作一个「池概况」而不是 6 张互不相干的卡片。 */}
      <div
        aria-label="凭证状态统计"
        className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3 lg:grid-cols-6"
      >
        <Metric label="凭证总数" value={formatNumber(counts.total)} icon={<Database className="size-4" />} className="border-0 bg-transparent py-0 ring-0" />
        <Metric label="可用" value={formatNumber(counts.ready)} tone="ok" icon={<CheckCircle2 className="size-4" />} className="border-0 bg-transparent py-0 ring-0" />
        <Metric label="冷却中" value={formatNumber(counts.cooling)} tone="warn" icon={<Timer className="size-4" />} className="border-0 bg-transparent py-0 ring-0" />
        <Metric label="已禁用" value={formatNumber(counts.disabled)} icon={<Ban className="size-4" />} className="border-0 bg-transparent py-0 ring-0" />
        <Metric label="额度耗尽" value={formatNumber(counts.exhausted)} tone="danger" icon={<BatteryLow className="size-4" />} className="border-0 bg-transparent py-0 ring-0" />
        <Metric label="已暂停" value={formatNumber(counts.off)} icon={<ToggleLeft className="size-4" />} className="border-0 bg-transparent py-0 ring-0" />
      </div>

      <div className="mt-4 border-t border-border pt-3">
        <div className="mb-2 text-xs text-muted-foreground">健康度四态分布</div>
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
      </div>
    </section>
  );
}
