import { useState } from "react";
import {
  Activity,
  ChevronRight,
  ExternalLink,
  RadioTower,
  RefreshCw,
  TriangleAlert,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useSessionContext } from "../Layout";
import { useUpstreamStatus } from "../api/hooks";
import {
  formatAgo,
  formatDuration,
  formatTime,
  upstreamLevelLabel,
  upstreamLevelTone,
} from "../api/display";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import {
  Badge,
  Button,
  Checkbox,
  EmptyState,
  Label,
  Notice,
  Panel,
} from "../ui";
import { safeHttpUrl } from "../lib/safeUrl";
import type {
  UpstreamIncident,
  UpstreamLevel,
  UpstreamSource,
  UpstreamStatusResponse,
} from "../api/types";

/**
 * 上游官方状态页聚合页（operator：admin / operator）。
 *
 * 定位：本服务自己的冷却只能回答「这个号现在不行」，回答不了「是上游整体挂了
 * 还是我这边凭证 / 额度的问题」。这一页把各模型 / 网关提供方的官方状态并排
 * 摆出来，补上另一半信息。**只做解释，不做决策**——状态页滞后于我们自己的
 * 实时冷却，看它不该改变调度行为。
 *
 * 数据是后端周期抓取的内存快照（重启归零），前端只轮询回读。
 *
 * 版式按「先结论、后明细」收敛（这一页要回答的是「现在哪里不对」，不是「每个源
 * 长什么样」）：
 * 1. 概况条——整体档位 + 档位分布 + 抓取运行态，一块表面说清全局；
 * 2. 源清单——一行一个源，行内直接给出档位与一句话摘要；**异常源默认展开**，
 *    正常源折成一行。原先的卡片墙在源高度差很大时会留出大片空洞（一个 25 项
 *    组件的 OpenAI 挨着一个 3 项的 MiniMax），改成整行列表后既没有排布空洞，
 *    也与「凭证管理」的分组行同一套交互语言。
 */

/** 分组标题与顺序：渠道级（与本站渠道一一对应）永远排前面。 */
const SCOPE_GROUPS = [
  { scope: "channel", label: "渠道（与本站入口一一对应）" },
  { scope: "model", label: "模型 / 上游（经渠道间接使用）" },
] as const;

/**
 * 图例里的档位顺序（严重度降序）。
 *
 * 与后端 `_LEVEL_RANK` 同序：图例是给人按「先看最糟的」扫的，不能按枚举顺序
 * 排（那样「正常」会排在最前面，把故障挤到右边）。
 */
const LEVEL_ORDER: UpstreamLevel[] = [
  "major",
  "partial",
  "degraded",
  "maintenance",
  "operational",
  "unknown",
];

/** 档位色调 → 分布条分段底色（与 `upstreamLevelTone` 一一对应）。 */
const TONE_BAR: Record<ReturnType<typeof upstreamLevelTone>, string> = {
  ok: "bg-ok",
  warn: "bg-warn",
  danger: "bg-destructive",
  muted: "bg-muted-foreground/40",
};

/** 分布条的分段顺序：故障在左，符合从左往右读的注意力顺序。 */
const TONE_ORDER: ReturnType<typeof upstreamLevelTone>[] = [
  "danger",
  "warn",
  "ok",
  "muted",
];

/** 展开后最多铺几行组件明细，其余折进「显示全部」。 */
const COMPONENT_CAP = 6;

/** 徽标：档位文字 + 色调；`source_stale` 单独加一枚，说明源侧自己声明数据过期。 */
function LevelBadge({ level }: { level: UpstreamLevel }) {
  return (
    <Badge tone={upstreamLevelTone(level)} dot>
      {upstreamLevelLabel(level)}
    </Badge>
  );
}

/** 行内摘要：一眼说清这个源「怎么了」，不必展开。 */
function sourceSummary(source: UpstreamSource): string {
  // 失败原因可能很长（`ReadTimeout: ...`），摘要只给结论，原文在展开态里
  if (source.error) return "本次抓取失败";
  const parts: string[] = [];
  if (source.components.length === 0) {
    parts.push("该源不提供逐组件状态");
  } else {
    const abnormal = source.components.filter(
      (component) => component.level !== "operational",
    ).length;
    parts.push(
      abnormal > 0
        ? `${source.components.length} 项明细，${abnormal} 项异常`
        : `${source.components.length} 项明细全部正常`,
    );
  }
  const ongoing = source.incidents.filter((incident) => incident.ongoing).length;
  if (ongoing > 0) parts.push(`${ongoing} 条进行中事故`);
  return parts.join(" · ");
}

/** 一条事故：进行中 / 已恢复 + 影响档位 + 更新时间 + 原文链接。 */
function IncidentRow({ incident }: { incident: UpstreamIncident }) {
  return (
    <li className="flex flex-col gap-1 border-t border-border/60 py-2 first:border-t-0 first:pt-0">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={incident.ongoing ? "danger" : "muted"} dot>
          {incident.ongoing ? "进行中" : "已恢复"}
        </Badge>
        <span
          className={cn(
            "text-xs font-medium",
            incident.ongoing ? "text-foreground" : "text-muted-foreground",
          )}
        >
          {incident.title}
        </span>
        {incident.ongoing ? (
          <Badge tone={upstreamLevelTone(incident.level)}>
            {upstreamLevelLabel(incident.level)}
          </Badge>
        ) : (
          // 已恢复事故的档位只是历史信息：给它上红色徽标会让人误以为还在故障
          <span className="text-xs text-muted-foreground">
            {upstreamLevelLabel(incident.level)}
          </span>
        )}
        {incident.updated_at !== null && (
          <span className="text-xs text-muted-foreground tabular-nums">
            {formatTime(incident.updated_at)}
          </span>
        )}
        {incident.url && safeHttpUrl(incident.url) && (
          <a
            href={safeHttpUrl(incident.url) ?? undefined}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-0.5 text-xs text-primary hover:underline"
          >
            原文
            <ExternalLink className="size-3" />
          </a>
        )}
      </div>
      {incident.detail && (
        <p className="text-xs leading-relaxed text-muted-foreground">{incident.detail}</p>
      )}
    </li>
  );
}

/**
 * 事故列表：进行中默认铺开，已恢复折进一个计数按钮。
 *
 * 各源都会挂着一串近期已恢复事故，全铺开时它们比真正要看的进行中条目还多。
 */
function IncidentList({ source }: { source: UpstreamSource }) {
  const [showRecovered, setShowRecovered] = useState(false);
  const ongoing = source.incidents.filter((item) => item.ongoing);
  const recovered = source.incidents.filter((item) => !item.ongoing);
  if (source.incidents.length === 0) return null;

  return (
    <div>
      <div className="mb-1 text-xs font-medium text-muted-foreground">近期事故</div>
      {ongoing.length === 0 ? (
        <p className="text-xs text-muted-foreground">无进行中事故。</p>
      ) : (
        <ul>
          {ongoing.map((incident, index) => (
            <IncidentRow key={`ongoing-${incident.title}-${index}`} incident={incident} />
          ))}
        </ul>
      )}
      {recovered.length > 0 && (
        <div className={cn(ongoing.length > 0 && "mt-1")}>
          <button
            type="button"
            aria-expanded={showRecovered}
            data-testid={`upstream-recovered-toggle-${source.key}`}
            onClick={() => setShowRecovered((value) => !value)}
            className="inline-flex cursor-pointer items-center gap-1 rounded-md text-xs text-muted-foreground hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
          >
            <ChevronRight
              className={cn("size-3.5 transition-transform", showRecovered && "rotate-90")}
              aria-hidden
            />
            {showRecovered ? "收起" : "展开"} {recovered.length} 条已恢复事故
          </button>
          {showRecovered && (
            <ul className="mt-1">
              {recovered.map((incident, index) => (
                <IncidentRow key={`recovered-${incident.title}-${index}`} incident={incident} />
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * 组件 / 模型明细。
 *
 * **异常项排前面**：截断到 `COMPONENT_CAP` 时，若按源侧原始顺序排（Statuspage
 * 按分组顺序给），真正要看的异常项可能正好落在第 7 项之后被折起来——上面一行
 * 写着「3 项异常」，展开却只看到 6 行「正常」。排序用稳定排序，同档位内保持
 * 源侧顺序（用户对「Kilo Website 在 Cloudflare 前面」有肌肉记忆）。
 *
 * OpenAI 的 25 项组件表能把一行撑到两屏高，故默认只铺前几项，其余按需展开。
 */
function ComponentList({ source }: { source: UpstreamSource }) {
  const [showAll, setShowAll] = useState(false);
  const total = source.components.length;
  if (total === 0) return null;
  const sorted = [...source.components].sort(
    (left, right) =>
      Number(left.level === "operational") - Number(right.level === "operational"),
  );
  const shown = showAll ? sorted : sorted.slice(0, COMPONENT_CAP);

  return (
    <div>
      <div className="mb-1 text-xs font-medium text-muted-foreground">
        组件明细（{total}）
      </div>
      <ul className="space-y-1">
        {shown.map((component) => (
          <li
            key={component.name}
            className="flex flex-wrap items-baseline justify-between gap-2 text-xs"
          >
            <span
              className={cn(
                component.level === "operational"
                  ? "text-muted-foreground"
                  : "text-foreground",
              )}
            >
              {component.name}
            </span>
            <span className="flex items-baseline gap-2">
              {component.detail && (
                <span className="text-muted-foreground tabular-nums">{component.detail}</span>
              )}
              {component.level === "operational" ? (
                // 「正常」是这一列的绝大多数取值：逐行铺同一枚绿色徽标等于没有
                // 信息，异常徽标反而被淹掉。留文字给读屏，视觉上以「无徽标」表示正常。
                <span className="sr-only">正常</span>
              ) : (
                <LevelBadge level={component.level} />
              )}
            </span>
          </li>
        ))}
      </ul>
      {total > COMPONENT_CAP && (
        <button
          type="button"
          onClick={() => setShowAll((value) => !value)}
          className="mt-1 cursor-pointer rounded-md text-xs text-primary hover:underline focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
        >
          {showAll ? `只看前 ${COMPONENT_CAP} 项` : `显示全部 ${total} 项`}
        </button>
      )}
    </div>
  );
}

/**
 * 一个源 = 一行。
 *
 * 展开态由「源是否异常」决定默认值，`override` 只记用户的手动选择：`null` 表示
 * 跟随规则（源从正常变成异常时会自动展开），布尔表示用户已表态、不再自动改。
 */
function SourceRow({ source }: { source: UpstreamSource }) {
  const [override, setOverride] = useState<boolean | null>(null);
  const open = override ?? source.level !== "operational";

  return (
    <li
      id={`upstream-row-${source.key}`}
      data-testid={`upstream-source-${source.key}`}
      className="scroll-mt-24"
    >
      <div className="flex items-start gap-2 px-4 py-2.5 transition-colors hover:bg-muted/40">
        <button
          type="button"
          aria-expanded={open}
          data-testid={`upstream-row-toggle-${source.key}`}
          onClick={() => setOverride(!open)}
          className="flex min-w-0 flex-1 cursor-pointer items-start gap-2.5 rounded-md text-left focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
        >
          <ChevronRight
            className={cn(
              "mt-0.5 size-4 shrink-0 text-muted-foreground transition-transform",
              open && "rotate-90",
            )}
            aria-hidden
          />
          <span className="min-w-0 flex-1">
            <span className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-medium text-foreground">{source.label}</span>
              <LevelBadge level={source.level} />
              {source.source_stale && <Badge tone="warn">源侧数据过期</Badge>}
            </span>
            <span
              className={cn(
                "mt-0.5 block text-xs",
                source.error ? "text-warn-ink" : "text-muted-foreground",
              )}
            >
              {sourceSummary(source)}
            </span>
          </span>
        </button>
        <a
          href={source.page_url}
          target="_blank"
          rel="noopener noreferrer"
          data-testid={`upstream-link-${source.key}`}
          className="mt-0.5 inline-flex shrink-0 items-center gap-1 text-xs text-primary hover:underline"
        >
          官方状态页
          <ExternalLink className="size-3" aria-hidden />
        </a>
      </div>

      {open && (
        <div className="space-y-3 border-t border-border/60 bg-muted/30 px-4 py-3 pl-10">
          {source.error && (
            <p
              data-testid={`upstream-error-${source.key}`}
              className="flex items-start gap-1.5 text-xs leading-relaxed text-muted-foreground"
            >
              {/* 抓取失败不是「上游挂了」：中性文字 + 警告图标，别抢真实故障的红 */}
              <TriangleAlert className="mt-px size-3.5 shrink-0 text-warn" aria-hidden />
              <span>本次抓取失败：{source.error}</span>
            </p>
          )}

          {source.components.length === 0 && !source.error && (
            <p className="text-xs text-muted-foreground">
              该源不提供逐组件状态，只有整体档位与事故记录。
            </p>
          )}

          <ComponentList source={source} />
          <IncidentList source={source} />
        </div>
      )}
    </li>
  );
}

/**
 * 概况条：整体档位 + 档位分布 + 抓取运行态。
 *
 * 三块信息合成一块表面——原先「四张指标卡 + 一个只有一行字的抓取状态面板」
 * 占了整整两屏顶部空间，而它们说的是同一件事（全局怎么样）。
 */
function OverviewStrip({
  data,
  onlyAbnormal,
  onToggleAbnormal,
  visibleCount,
  isFetching,
  onRefresh,
}: {
  data: UpstreamStatusResponse;
  onlyAbnormal: boolean;
  onToggleAbnormal: (value: boolean) => void;
  visibleCount: number;
  isFetching: boolean;
  onRefresh: () => void;
}) {
  const counts = new Map<UpstreamLevel, number>();
  const toneCounts: Record<ReturnType<typeof upstreamLevelTone>, number> = {
    ok: 0,
    warn: 0,
    danger: 0,
    muted: 0,
  };
  for (const source of data.sources) {
    counts.set(source.level, (counts.get(source.level) ?? 0) + 1);
    toneCounts[upstreamLevelTone(source.level)] += 1;
  }
  const total = data.sources.length;
  const legend = LEVEL_ORDER.filter((level) => (counts.get(level) ?? 0) > 0);
  const barLabel = legend
    .map((level) => `${upstreamLevelLabel(level)} ${counts.get(level) ?? 0}`)
    .join("，");

  return (
    <section
      className="rounded-xl bg-card px-4 py-4 shadow-sm ring-1 ring-border"
      data-testid="upstream-overview"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <div className="flex items-center gap-2">
          <span className="grid size-7 place-items-center rounded-lg bg-muted text-muted-foreground">
            <Activity className="size-4" aria-hidden />
          </span>
          <span className="text-sm font-semibold tracking-tight text-foreground">上游概况</span>
          <LevelBadge level={data.summary.level} />
        </div>
        <div className="text-xs text-muted-foreground">
          最差的源决定整体档位（共 {data.summary.total} 个源）
        </div>
      </div>

      {/* 分布条与图例同源（都从 `upstreamLevelTone` 派生）：分段只是图例的视觉
          强化，含义由图例文字与 aria-label 承担（颜色不单独表意）。 */}
      <div
        role="img"
        aria-label={`上游档位分布：${barLabel || "暂无数据"}，共 ${total} 个源`}
        className="mt-3 flex h-2.5 w-full overflow-hidden rounded-full bg-muted"
      >
        {TONE_ORDER.map((tone) =>
          toneCounts[tone] > 0 ? (
            <div
              key={tone}
              className={cn("transition-all", TONE_BAR[tone])}
              style={{ width: `${(toneCounts[tone] / Math.max(1, total)) * 100}%` }}
            />
          ) : null,
        )}
      </div>

      <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1.5 text-sm">
        {legend.map((level) => (
          <span key={level} className="inline-flex items-center gap-1.5">
            <span
              className={cn("size-2 rounded-full", TONE_BAR[upstreamLevelTone(level)])}
              aria-hidden
            />
            <span className="text-muted-foreground">{upstreamLevelLabel(level)}</span>
            <strong className="tabular-nums">{counts.get(level) ?? 0}</strong>
          </span>
        ))}
        {total === 0 && <span className="text-muted-foreground">尚未抓取到任何源</span>}
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-2 border-t border-border/70 pt-3 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <RefreshCw className="size-3.5" aria-hidden />
          上次抓取：
          <span className="text-foreground">
            {data.fetched_at
              ? `${formatAgo(data.fetched_at, data.server_time)}（${formatTime(data.fetched_at)}）`
              : "尚未抓取"}
          </span>
        </span>
        <span>抓取周期：{formatDuration(data.interval_seconds)}</span>
        <Label className="w-fit cursor-pointer gap-1.5 text-xs font-normal text-muted-foreground">
          <Checkbox
            checked={onlyAbnormal}
            data-testid="upstream-abnormal-toggle"
            onCheckedChange={(value) => onToggleAbnormal(value === true)}
          />
          只看异常
        </Label>
        {/* 过滤后的条数变化要被读屏播报，否则勾上开关只有视觉变化 */}
        <span role="status" data-testid="upstream-hidden-count" className="text-xs text-muted-foreground">
          {onlyAbnormal ? `已隐藏 ${total - visibleCount} 个正常源` : ""}
        </span>
        <Button
          variant="default"
          size="sm"
          aria-busy={isFetching}
          disabled={isFetching}
          data-testid="upstream-refresh"
          onClick={onRefresh}
          className="ml-auto"
        >
          <RefreshCw className={cn("size-3.5", isFetching && "animate-spin")} aria-hidden />
          刷新
        </Button>
      </div>
    </section>
  );
}

/**
 * 需要关注：非正常源压成一排短标签，点一下跳到对应行。
 *
 * 只在「只看异常」关闭时渲染——开关打开后清单本身已经只剩异常源，再列一遍
 * 就是同一份内容说两次。全部正常时整块不渲染（概况条的档位已经说了）。
 *
 * 有它是因为清单按 `SOURCE_SPECS` 顺序排（顺序稳定，用户能记住每个源在哪），
 * 异常源可能排在第 7 行；这一条把「现在哪里不对」提到最前面。
 */
function AttentionStrip({ sources }: { sources: UpstreamSource[] }) {
  if (sources.length === 0) return null;

  return (
    <section
      className="rounded-xl bg-card px-4 py-3 shadow-sm ring-1 ring-border"
      data-testid="upstream-attention"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <span className="text-sm font-semibold tracking-tight text-foreground">需要关注</span>
        <span className="text-xs text-muted-foreground">{sources.length} 个源非正常</span>
      </div>
      <ul className="mt-2.5 flex flex-wrap gap-1.5">
        {sources.map((source) => {
          const worst = source.components.find((item) => item.level !== "operational");
          const ongoing = source.incidents.find((item) => item.ongoing);
          // 抓取失败 → 最差组件 → 进行中事故 → 只有档位可报
          const reason = source.error ? "抓取失败" : (worst?.name ?? ongoing?.title ?? null);
          return (
            <li key={source.key} className="max-w-full">
              <a
                href={`#upstream-row-${source.key}`}
                className="inline-flex max-w-full items-center gap-1.5 rounded-lg border border-border/70 bg-muted/40 px-2 py-1 text-xs transition-colors hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
              >
                <LevelBadge level={source.level} />
                <span className="font-medium text-foreground">{source.label}</span>
                {reason && <span className="truncate text-muted-foreground">{reason}</span>}
              </a>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

export function UpstreamStatusPage() {
  const session = useSessionContext();
  // 60 秒轮询：上游状态是「现在怎么样」，用户停留时自动刷新。
  const { data, isLoading, isError, isFetching, refetch } = useUpstreamStatus(
    session.username,
    60_000,
  );
  // 只看异常：默认关闭（正常源占多数，全量展示才有「全局都正常」的对照）。
  const [onlyAbnormal, setOnlyAbnormal] = useState(false);

  if (isLoading) {
    return (
      <div data-testid="upstream-status-page">
        <PageSkeleton variant="cards" />
      </div>
    );
  }

  const sources = data?.sources ?? [];
  // 「异常」= 非 operational（含抓取失败的 unknown）：与后端 SourceStatus.abnormal
  // 同一口径。抓不到也是一种要看的异常，不能因为它不是「上游挂了」就藏起来。
  const visible = onlyAbnormal
    ? sources.filter((source) => source.level !== "operational")
    : sources;

  return (
    <div className="space-y-6" data-testid="upstream-status-page">
      <PageHeader
        eyebrow="运维"
        title="上游状态"
        description="各模型 / 网关提供方的官方状态页聚合：判断报错是上游整体故障，还是本服务的凭证 / 额度问题。"
        icon={<RadioTower className="size-5" />}
      />

      {isError ? (
        <div data-testid="upstream-status-error">
          <Notice tone="danger">
            上游状态加载失败，请刷新重试；若刚升级服务，请确认后端已重启。
          </Notice>
        </div>
      ) : (
        <>
          {data && (
            <OverviewStrip
              data={data}
              onlyAbnormal={onlyAbnormal}
              onToggleAbnormal={setOnlyAbnormal}
              visibleCount={visible.length}
              isFetching={isFetching}
              onRefresh={() => void refetch()}
            />
          )}

          {data && !data.enabled && (
            <div data-testid="upstream-disabled">
              <Notice tone="warn">
                后台抓取已关闭（「任务与配置」可重新打开）。以下为最后一次快照，可能已经过时。
              </Notice>
            </div>
          )}

          {!onlyAbnormal && (
            <AttentionStrip
              sources={sources.filter((source) => source.level !== "operational")}
            />
          )}

          {sources.length === 0 ? (
            <Panel title="上游状态">
              <EmptyState
                icon={<RadioTower className="size-5" />}
                title="尚未抓取到状态"
                description={
                  data?.enabled
                    ? "后台任务会在启动后立刻抓一轮（默认每 5 分钟一次），稍后刷新即可看到各上游的状态。"
                    : "后台抓取任务当前已关闭，可在「任务与配置」页重新打开。"
                }
                data-testid="no-upstream-status"
              />
            </Panel>
          ) : visible.length === 0 ? (
            <Panel title="上游状态">
              <EmptyState
                icon={<RadioTower className="size-5" />}
                title="当前没有异常源"
                description="所有上游状态正常。取消「只看异常」可查看全部源。"
                data-testid="no-upstream-abnormal"
              />
            </Panel>
          ) : (
            SCOPE_GROUPS.map(({ scope, label }) => {
              const rows = visible.filter((source) => source.scope === scope);
              if (rows.length === 0) return null;
              return (
                <section key={scope} className="space-y-2">
                  <h2 className="flex items-baseline gap-2 text-sm font-semibold tracking-tight text-foreground">
                    <span>{label}</span>
                    <span className="text-xs font-normal text-muted-foreground tabular-nums">
                      {rows.length}
                    </span>
                  </h2>
                  {/* 整行列表（divide-y）而不是卡片网格：源之间的高度差很大，
                      网格会在短卡旁边留出大片空洞 */}
                  <ul
                    className="divide-y divide-border/70 overflow-hidden rounded-xl bg-card shadow-sm ring-1 ring-border"
                    aria-label={label}
                  >
                    {rows.map((source) => (
                      <SourceRow key={source.key} source={source} />
                    ))}
                  </ul>
                </section>
              );
            })
          )}

          <Notice tone="muted">
            官方状态页粒度比本服务的模型级冷却粗、更新也滞后，这里只做解释，不参与选号 /
            熔断；本服务实时冷却见「凭证管理」页。数据仅存内存，服务重启后归零。
          </Notice>
        </>
      )}
    </div>
  );
}
