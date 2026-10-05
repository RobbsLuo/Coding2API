import {
  Ban,
  BatteryLow,
  CheckCircle2,
  CircleMinus,
  Database,
  HelpCircle,
  Timer,
  ToggleLeft,
} from "lucide-react";
import { formatNumber, healthView, credentialState } from "../api/display";
import type { Credential } from "../api/types";
import { Metric } from "../ui";

/**
 * 池概况：一张卡里说清「这个池现在能不能用」。
 *
 * 主轴是**健康度四态**（已知剩余 / 未探测 / 无探测 / 已耗尽），四态互斥且
 * 合计等于凭证总数——调度器就是按健康度选号的，所以它才是池的整体口径。
 * 「已耗尽」与旧版状态格里的「额度耗尽」是同一批凭证，不再重复计数。
 *
 * 冷却中 / 已禁用 / 已暂停不是第四类健康度，而是叠加在上面的**不可用原因**，
 * 数量少、只看有没有，因此收成底部一行摘要，不与四态抢主视觉。
 * `now` 由调用方传入（凭证页已有 `Date.now()/1000`）。
 */
export function PoolOverview({ credentials, now }: {
  credentials: Credential[];
  now: number;
}) {
  const healthTally = { known: 0, unknown: 0, noprobe: 0, exhausted: 0 };
  const blocked = { cooling: 0, disabled: 0, off: 0 };
  for (const credential of credentials) {
    healthTally[healthView(credential.health, credential.provider).kind] += 1;
    const state = credentialState(credential, now);
    if (state === "cooling") blocked.cooling += 1;
    else if (state === "disabled") blocked.disabled += 1;
    else if (state === "off") blocked.off += 1;
  }
  const total = credentials.length;
  const hasBlocked = blocked.cooling + blocked.disabled + blocked.off > 0;

  return (
    <section
      className="rounded-xl bg-card px-4 py-4 ring-1 ring-border"
      data-testid="pool-overview"
    >
      <div className="mb-3 text-sm font-medium">池概况</div>

      {/* 分布条：一眼看出池里还有多少能打的号（比例 = 四态 / 总数） */}
      <div
        role="img"
        aria-label={`健康度分布：已知剩余 ${healthTally.known}，未探测 ${healthTally.unknown}，无探测 ${healthTally.noprobe}，已耗尽 ${healthTally.exhausted}`}
        className="flex h-2 w-full overflow-hidden rounded-full bg-muted"
      >
        {healthTally.known > 0 && (
          <div
            className="bg-ok transition-all"
            style={{ width: `${(healthTally.known / Math.max(1, total)) * 100}%` }}
          />
        )}
        {healthTally.unknown > 0 && (
          <div
            className="bg-warn transition-all"
            style={{ width: `${(healthTally.unknown / Math.max(1, total)) * 100}%` }}
          />
        )}
        {healthTally.noprobe > 0 && (
          <div
            className="bg-muted-foreground/40 transition-all"
            style={{ width: `${(healthTally.noprobe / Math.max(1, total)) * 100}%` }}
          />
        )}
        {healthTally.exhausted > 0 && (
          <div
            className="bg-destructive transition-all"
            style={{ width: `${(healthTally.exhausted / Math.max(1, total)) * 100}%` }}
          />
        )}
      </div>

      {/* 四态 + 总数：无边框格子并进同一卡片（Metric 的 className 去掉 Card 外观） */}
      <div
        aria-label="凭证健康度统计"
        className="mt-3 grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3 lg:grid-cols-5"
      >
        <div data-testid="pool-total" className="min-w-0">
          <Metric label="凭证总数" value={formatNumber(total)} icon={<Database className="size-4" />} className="border-0 bg-transparent py-0 ring-0" />
        </div>
        <div data-testid="health-known" className="min-w-0">
          <Metric label="可用" value={formatNumber(healthTally.known)} tone="ok" icon={<CheckCircle2 className="size-4" />} className="border-0 bg-transparent py-0 ring-0" />
        </div>
        <div data-testid="health-unknown" className="min-w-0">
          <Metric label="未探测" value={formatNumber(healthTally.unknown)} tone="warn" icon={<HelpCircle className="size-4" />} className="border-0 bg-transparent py-0 ring-0" />
        </div>
        <div data-testid="health-noprobe" className="min-w-0">
          <Metric label="无探测" value={formatNumber(healthTally.noprobe)} icon={<CircleMinus className="size-4" />} className="border-0 bg-transparent py-0 ring-0" />
        </div>
        <div data-testid="health-exhausted" className="min-w-0">
          <Metric label="已耗尽" value={formatNumber(healthTally.exhausted)} tone="danger" icon={<BatteryLow className="size-4" />} className="border-0 bg-transparent py-0 ring-0" />
        </div>
      </div>

      <p className="mt-3 text-xs text-muted-foreground">
        「未探测」＝探测失败或渠道未给额度信息（点行内「探测」可重试），≠已耗尽；
        「无探测」是 OpenCode Zen / Kilo Gateway 免费层，上游没有额度接口，探也没用。
        调度器优先选可用里剩余百分比高者，打平时再用账户剩余积分多者。
      </p>

      {/* 不可用原因摘要：与四态重叠（叠加状态），故不另起统计格 */}
      {hasBlocked && (
        <div
          className="mt-3 flex flex-wrap gap-x-5 gap-y-1.5 border-t border-border pt-3 text-xs text-muted-foreground"
          data-testid="pool-blocked"
        >
          {blocked.cooling > 0 && (
            <span data-testid="blocked-cooling" className="inline-flex items-center gap-1.5 text-warn">
              <Timer className="size-3.5" />
              冷却中 <strong>{blocked.cooling}</strong>
            </span>
          )}
          {blocked.disabled > 0 && (
            <span data-testid="blocked-disabled" className="inline-flex items-center gap-1.5">
              <Ban className="size-3.5" />
              已禁用 <strong>{blocked.disabled}</strong>
            </span>
          )}
          {blocked.off > 0 && (
            <span data-testid="blocked-off" className="inline-flex items-center gap-1.5">
              <ToggleLeft className="size-3.5" />
              已暂停 <strong>{blocked.off}</strong>
            </span>
          )}
        </div>
      )}
    </section>
  );
}
