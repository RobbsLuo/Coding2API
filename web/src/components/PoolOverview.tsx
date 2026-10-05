import { Ban, Timer, ToggleLeft } from "lucide-react";
import { formatNumber, healthView, credentialState } from "../api/display";
import type { Credential } from "../api/types";

/**
 * 池概况：一条分布条 + 两行摘要，回答「这个池现在还剩多少能打的号」。
 *
 * 主轴是**健康度四态**（可用 / 未探测 / 无探测 / 已耗尽），四态互斥且合计
 * 等于凭证总数——调度器就是按健康度选号的，所以它才是池的整体口径。
 * 「已耗尽」与旧版状态格里的「额度耗尽」是同一批凭证，不重复计数。
 *
 * 不再摆统计格：数字全在图例里（`可用 8 / 12`），再单列一遍是重复。
 * 冷却中 / 已禁用 / 已暂停是叠加在四态之上的**不可用原因**，同样收进摘要行。
 * 四态语义（未探测 vs 无探测的区别）由表格「状态 / 健康度」列头的
 * ColumnHint 承载，这里不复述。
 *
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
      {/* 分布条：一眼看出池里还有多少能打的号（比例 = 四态 / 总数） */}
      <div
        role="img"
        aria-label={`健康度分布：可用 ${healthTally.known}，未探测 ${healthTally.unknown}，无探测 ${healthTally.noprobe}，已耗尽 ${healthTally.exhausted}，共 ${total} 个凭证`}
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

      <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1.5 text-sm">
        <span data-testid="health-known" className="inline-flex items-center gap-1.5">
          <span className="size-2 rounded-full bg-ok" />
          可用 <strong>{healthTally.known}</strong>
          <span className="text-muted-foreground">/ {formatNumber(total)}</span>
        </span>
        {healthTally.unknown > 0 && (
          <span data-testid="health-unknown" className="inline-flex items-center gap-1.5 text-muted-foreground">
            <span className="size-2 rounded-full bg-warn" />
            未探测 <strong>{healthTally.unknown}</strong>
          </span>
        )}
        {healthTally.noprobe > 0 && (
          <span data-testid="health-noprobe" className="inline-flex items-center gap-1.5 text-muted-foreground">
            <span className="size-2 rounded-full bg-muted-foreground/40" />
            无探测 <strong>{healthTally.noprobe}</strong>
          </span>
        )}
        {healthTally.exhausted > 0 && (
          <span data-testid="health-exhausted" className="inline-flex items-center gap-1.5 text-destructive">
            <span className="size-2 rounded-full bg-destructive" />
            已耗尽 <strong>{healthTally.exhausted}</strong>
          </span>
        )}
        {total === 0 && <span className="text-muted-foreground">池里还没有凭证</span>}
      </div>

      {/* 不可用原因摘要：与四态重叠（叠加状态），全部为 0 时整块不渲染 */}
      {hasBlocked && (
        <div
          className="mt-2 flex flex-wrap gap-x-5 gap-y-1.5 text-xs text-muted-foreground"
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
