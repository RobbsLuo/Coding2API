import {
  Activity,
  Ban,
  CalendarCheck,
  CalendarClock,
  ChevronRight,
  CircleCheck,
  Coins,
  Flame,
  Hourglass,
  Sparkles,
  Timer,
  ToggleLeft,
} from "lucide-react";
import { cn } from "@/lib/utils";
import {
  formatDuration,
  formatNumber,
  formatTimeParts,
  QUOTA_UNIT,
  STATE_LABEL,
} from "../../api/display";
import type { CredentialState } from "../../api/display";
import { STATE_ORDER } from "../../lib/credentialList";
import type { ProviderGroup } from "../../lib/credentialList";
import { rateLabel } from "../../components/ModelPicker";
import type { ProviderModel } from "../../components/ModelPicker";
import { ProviderIcon } from "../../components/ProviderIcon";
import { LongTextTip } from "../../components/Tip";
import { Checkbox, TableCell, TableRow } from "../../ui";
import { QuotaRing } from "./cells";
import { supportsCheckin } from "./helpers";

/**
 * 组头状态分布的图标：与行内状态徽章的语义色同源，图形本身也表意
 * （色 + 图形 + 文字三重冗余，不单靠颜色）。
 */
const STATE_ICON: Record<CredentialState, typeof CircleCheck> = {
  ready: CircleCheck,
  cooling: Timer,
  disabled: Ban,
  off: ToggleLeft,
  exhausted: Flame,
};

/** 组头状态分布的文字色：`-ink` 变体保证在浅底 / 深底上都够对比。 */
const STATE_TEXT: Record<CredentialState, string> = {
  ready: "text-ok",
  cooling: "text-warn-ink",
  disabled: "text-danger-ink",
  off: "text-muted-foreground",
  exhausted: "text-danger-ink",
};

/**
 * 一个渠道的组头：折叠时的「渠道仪表盘」。
 *
 * 展开态、选中态与筛选都仍由页面壳持有，这里只接收已经算好的派生值
 * （模型清单、额度占比、是否全选）并渲染，判定口径与行内保持一致。
 */
export function CredentialGroupHeader({
  group,
  isCollapsed,
  canWrite,
  allSelected,
  groupIds,
  groupModels,
  rateText,
  quotaPct,
  expiryWindow,
  expirySecondaryWindow,
  onToggleGroup,
  onToggleGroupSelected,
}: {
  group: ProviderGroup;
  isCollapsed: boolean;
  canWrite: boolean;
  allSelected: boolean;
  groupIds: string[];
  groupModels: ProviderModel[];
  rateText: string | null;
  quotaPct: number | null;
  expiryWindow: number;
  expirySecondaryWindow: number;
  onToggleGroup: (key: string) => void;
  onToggleGroupSelected: (ids: string[], checked: boolean) => void;
}) {
  // 到期积分：以「窗口 + 数字」两个图块呈现，比一行长句更像仪表盘。
  // 主/次窗口措辞区分开（主窗口 36h 是次窗口 7 天的子集）。
  // 措辞必须点明「窗口」：它是调度窗口（36h / 7d），不是某笔额度的
  // 剩余到期时间——实测「1.5天」被读成倒计时（积分今天到期却显示 1.5 天）。
  const primaryWindowLabel = `${formatDuration(expiryWindow)}窗口内到期`;
  const secondaryWindowLabel = `${formatDuration(expirySecondaryWindow)}窗口内到期`;
  // 到期积分按本组那本账取：CodeArts（credits）的到期阶梯与其余渠道（pool）
  // 同源，直接对通用字段求和即可；免费层无额度接口，恒无到期积分。
  const hasExpiring = (group.expiring ?? 0) > 0;
  // 次窗口只在**比主窗口多出东西**时才展示：次窗口是主窗口的超集，
  // 相等就意味着「没有超出 1.5 天窗口的部分」。
  const hasExpiringSecondary =
    (group.expiringSecondary ?? 0) > (group.expiring ?? 0);
  // 列数随「是否显示操作列」与「选择列」变化：表格是 auto 布局，
  // 列数一变就重算列宽，展开的下一组会整列错位。选择列只在可写
  // （canWrite）时出现，与 ColumnHeaderRow / Row 的渲染条件严格一致。
  // canWrite：选择 + 凭证 + 状态 + 额度 + token + 成长 + 操作 = 7；
  // viewer：去掉选择与操作两列 = 5。
  const columnCount = canWrite ? 7 : 5;
  // 今日签到：分母是「该签的号」（暂停 / 硬禁用已由聚合跳过）。
  // 渠道本就不支持签到（Zen / Kilo / 国际站）时整块不渲染——
  // 永远 0/N 的仪表会被读成「这些号今天漏签了」。
  const checkin = group.checkin;
  const checkinTotal = checkin.done + checkin.failed + checkin.pending;
  const showCheckin = supportsCheckin(group.provider) && checkinTotal > 0;
  // 三态配色：有失败先红（那才是要处置的），全签完才绿。
  const checkinIconClass = checkin.failed > 0
    ? "bg-destructive/12 text-danger-ink ring-1 ring-destructive/25"
    : checkin.done === checkinTotal
      ? "bg-ok/12 text-ok-ink ring-1 ring-ok/25"
      : "bg-background text-muted-foreground ring-1 ring-border/70";
  const checkinTitle = checkin.failed > 0
    ? `今日签到（北京时间）：已签 ${checkin.done} / ${checkinTotal}，失败 ${checkin.failed}，未签 ${checkin.pending}。失败原因见行内「签到记录」。`
    : `今日签到（北京时间）：已签 ${checkin.done} / ${checkinTotal}，未签 ${checkin.pending}（任务未轮到也算未签）。`;
  return (
    // 组头：折叠时的「渠道仪表盘」——身份 + 状态分布 + 额度合计 +
    // 1.5 天 / 7 天内将过期的积分 + 今日签到（最右）。每个指标一个带
    // 图标的图块，比一行纯文字更好扫读；点击展开该渠道（默认全折叠）。
    <TableRow data-testid={`group-${group.key}`}>
                          <TableCell
                            colSpan={columnCount}
                            className="bg-muted/40 p-0"
                          >
                            {/* 组头「全选本渠道」的 checkbox 必须在 button **外面**：
                                checkbox 嵌进 button 会形成嵌套交互元素，读屏与键盘
                                焦点都会打架（点 checkbox 会连带展开/折叠该组）。
                                只在**会话可写**时渲染（viewer 整张表都没有选择列，组头
                                也不该多出这一格）。 */}
                            <div className="flex items-center">
                              {canWrite && (
                                <span className="pl-3">
                                  <Checkbox
                                    checked={allSelected}
                                    aria-label={`全选 ${group.label} 的账号`}
                                    data-testid={`group-select-${group.key}`}
                                    onCheckedChange={(checked) =>
                                      onToggleGroupSelected(groupIds, checked === true)}
                                  />
                                </span>
                              )}
                              <button
                                type="button"
                                data-testid={`group-toggle-${group.key}`}
                                aria-expanded={!isCollapsed}
                                onClick={() => onToggleGroup(group.key)}
                                className="flex min-w-0 flex-1 cursor-pointer flex-wrap items-center gap-x-4 gap-y-2 px-3 py-2 text-left focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
                              >
                              {/* 身份：折叠箭头 + 品牌图标 + 渠道名 + 账号数 */}
                              <span className="flex w-48 shrink-0 items-center gap-2">
                                <ChevronRight
                                  className={cn(
                                    "size-4 shrink-0 text-muted-foreground transition-transform",
                                    !isCollapsed && "rotate-90",
                                  )}
                                  aria-hidden
                                />
                                <span className="grid size-7 shrink-0 place-items-center rounded-md bg-background ring-1 ring-border/70">
                                  <ProviderIcon provider={group.provider} size={16} />
                                </span>
                                <span className="min-w-0">
                                  <span className="block truncate text-[13px] leading-tight font-semibold text-foreground">
                                    {group.label}
                                  </span>
                                  <span className="block text-xs leading-tight text-muted-foreground tabular-nums">
                                    {group.items.length} 个账号
                                  </span>
                                </span>
                              </span>

                              {/* 指标区：状态 / 额度 / 到期，每个都是「图标 + 标签 + 数值」。
                                  每块**固定宽度**（`w-* shrink-0`），否则内容宽度随渠道变化，
                                  各组的「状态 / 额度 / 到期」起点会各错各的、读起来不成列。
                                  窄屏放不下时按整块换行，块内仍保持同一套对齐。
                                  （今日签到在右簇，与模型一起贴右，见折叠按钮之后。） */}
                              <span className="flex flex-wrap items-center gap-x-4 gap-y-2">
                                {/* 状态分布：只列出现过的态，图标语义与行内状态徽章同源 */}
                                <span className="flex w-40 shrink-0 items-center gap-2">
                                  <span className="grid size-7 shrink-0 place-items-center rounded-md bg-background text-muted-foreground ring-1 ring-border/70">
                                    <Activity className="size-3.5" aria-hidden />
                                  </span>
                                  <span className="min-w-0">
                                    <span className="block text-[11px] leading-tight text-muted-foreground">状态</span>
                                    <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                                      {STATE_ORDER.filter(
                                        (state) => (group.states[state] ?? 0) > 0,
                                      ).map((state) => {
                                        const StateIcon = STATE_ICON[state];
                                        return (
                                          <span
                                            key={state}
                                            className="inline-flex items-center gap-1 text-xs leading-tight"
                                            data-testid={`group-${group.key}-state-${state}`}
                                          >
                                            <StateIcon
                                              className={cn("size-3.5", STATE_TEXT[state])}
                                              aria-hidden
                                            />
                                            <span className={STATE_TEXT[state]}>
                                              {STATE_LABEL[state]}
                                            </span>
                                            <span className="tabular-nums text-muted-foreground">
                                              {" "}
                                              {group.states[state]}
                                            </span>
                                          </span>
                                        );
                                      })}
                                    </span>
                                  </span>
                                </span>

                                {/* 额度合计：占比环 + 剩余 / 总量（标签行带精确百分比）。
                                    免费层（zen / kilo）无额度接口，null 显示 — 且不出环，
                                    退回一个中性图标位保持各块对齐。 */}
                                <span className="flex w-48 shrink-0 items-center gap-2">
                                  {quotaPct !== null ? (
                                    <span data-testid={`group-${group.key}-quota-pct`}>
                                      <QuotaRing pct={quotaPct} />
                                    </span>
                                  ) : (
                                    <span className="grid size-7 shrink-0 place-items-center rounded-md bg-muted text-muted-foreground ring-1 ring-border/70">
                                      <Coins className="size-3.5" aria-hidden />
                                    </span>
                                  )}
                                  <span
                                    className="min-w-0"
                                    title={group.ledger === "credits"
                                      ? "组内各号**积分账**合计（内置模型计费）；未探测到的号不计入"
                                      : "组内各号额度合计（只统计有额度接口的号；免费层 —）"}
                                  >
                                    <span className="block text-[11px] leading-tight text-muted-foreground">
                                      {group.ledger === "credits" ? "积分合计" : "额度合计"}
                                      {quotaPct !== null && (
                                        <span className="ml-1 tabular-nums text-ok-ink">
                                          {Math.round(quotaPct)}%
                                        </span>
                                      )}
                                    </span>
                                    <span className="block truncate text-xs leading-tight font-medium tabular-nums text-foreground">
                                      {group.remaining === null
                                        ? "—"
                                        : formatNumber(group.remaining)}
                                      {group.total !== null && (
                                        <span className="font-normal text-muted-foreground">
                                          {" / "}
                                          {formatNumber(group.total)}
                                        </span>
                                      )}
                                    </span>
                                  </span>
                                </span>

                                {/* 到期积分：主窗口（1.5 天）与次窗口（7 天）各一块。
                                    缺席的窗口不渲染（轨道留空），前几块的对齐不受影响。 */}
                                {hasExpiring && (
                                  <span
                                    className="flex w-36 shrink-0 items-center gap-2"
                                    data-testid={`group-${group.key}-expiring`}
                                    title={`组内各号主窗口（${formatDuration(expiryWindow)}）内将到期的积分合计（调度窗口，非单笔到期时刻）`}
                                  >
                                    <span className="grid size-7 shrink-0 place-items-center rounded-md bg-warn/12 text-warn ring-1 ring-warn/25">
                                      <Hourglass className="size-3.5" aria-hidden />
                                    </span>
                                    <span className="min-w-0">
                                      <span className="block text-[11px] leading-tight text-warn-ink">
                                        {primaryWindowLabel}
                                      </span>
                                      <span className="block text-xs leading-tight font-medium tabular-nums text-warn-ink">
                                        {formatNumber(group.expiring ?? 0)} {QUOTA_UNIT}
                                      </span>
                                    </span>
                                  </span>
                                )}
                                {hasExpiringSecondary && (
                                  <span
                                    className="flex w-36 shrink-0 items-center gap-2"
                                    data-testid={`group-${group.key}-expiring-secondary`}
                                    title={`组内各号次窗口（${formatDuration(expirySecondaryWindow)}）内将到期的积分合计，含主窗口的部分（调度窗口，非单笔到期时刻）`}
                                  >
                                    <span className="grid size-7 shrink-0 place-items-center rounded-md bg-warn/12 text-warn ring-1 ring-warn/25">
                                      <CalendarClock className="size-3.5" aria-hidden />
                                    </span>
                                    <span className="min-w-0">
                                      <span className="block text-[11px] leading-tight text-warn-ink">
                                        {secondaryWindowLabel}
                                      </span>
                                      <span className="block text-xs leading-tight font-medium tabular-nums text-warn-ink">
                                        {formatNumber(group.expiringSecondary ?? 0)} {QUOTA_UNIT}
                                      </span>
                                    </span>
                                  </span>
                                )}
                              </span>
                            </button>
                            {/* 右簇：今日签到 + 模型。两者都贴右，和模型成一对——
                                「这渠道今天签成功没有、还能打什么模型」是同一类收尾确认，
                                放在指标区外面还免去它与额度块抢宽度（窄屏时指标区先换行，
                                右簇始终贴边）。
                                签到「已签 / 该签」是主数字；失败单独标红——那才是要处置
                                的东西（失败在 30s 轮询里会随下一轮补签自动回落）。
                                最近时刻只给 HH:MM，日期对「今天」是冗余的。 */}
                            <span className="flex shrink-0 items-center">
                              {showCheckin && (
                                <span
                                  className="flex w-40 shrink-0 items-center gap-2 pr-3"
                                  data-testid={`group-${group.key}-checkin`}
                                  title={checkinTitle}
                                >
                                  <span className={cn(
                                    "grid size-7 shrink-0 place-items-center rounded-md",
                                    checkinIconClass,
                                  )}>
                                    <CalendarCheck className="size-3.5" aria-hidden />
                                  </span>
                                  <span className="min-w-0">
                                    <span className="block text-[11px] leading-tight text-muted-foreground">
                                      今日签到
                                    </span>
                                    <span className="block truncate text-xs leading-tight font-medium tabular-nums text-foreground">
                                      {checkin.done}/{checkinTotal}
                                      {checkin.failed > 0 && (
                                        <span className="ml-1.5 font-normal text-danger-ink">
                                          {checkin.failed} 失败
                                        </span>
                                      )}
                                      {checkin.lastAt !== null && (
                                        <span className="ml-1.5 font-normal text-muted-foreground">
                                          {formatTimeParts(checkin.lastAt)[1].slice(0, 5)}
                                        </span>
                                      )}
                                    </span>
                                  </span>
                                </span>
                              )}
                              {/* 本渠道当前能提供的模型：只给数量，全量清单进 hover。
                                  模型多的渠道有几十条，铺在组头会把这一行撑成十几行；
                                  而「几个模型、最便宜多少」正是判断这渠道值不值得展开的
                                  第一信息。清单里每条带**该渠道的**倍率（与模型选择器
                                  同口径，多渠道模型各渠道倍率不同，不能拿合并值冒充）。
                                  取不到模型列表时整块不渲染（不显示「0 个」——那会被读成
                                  这渠道没有模型，实际只是列表还没回来）。

                                  **必须在折叠按钮外面**：它自己是个可聚焦按钮（键盘能
                                  打开清单），按钮套按钮会让 HTML 解析直接拆掉外层结构。

                                  触屏没有 hover，点击又会被 Radix 的 pointerdown 关掉，
                                  所以移动端打不开清单（与页面其它 hover 明细同一限制）；
                                  真要在手机上看，需要换成 Popover（点击开合）。 */}
                              {groupModels.length > 0 && (
                                <LongTextTip
                                  className="w-80 max-w-[min(90vw,20rem)] p-0"
                                  content={
                                    <div data-testid={`group-${group.key}-models`}>
                                      {/* 头部：渠道身份 + 条数，吸顶不随清单滚走——
                                          几十条模型滚到中段时仍知道「这是哪个渠道的清单」 */}
                                      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
                                        <ProviderIcon provider={group.provider} size={14} />
                                        <span className="min-w-0 flex-1 truncate text-xs font-semibold text-foreground">
                                          {group.label}
                                        </span>
                                        <span className="shrink-0 rounded-full bg-muted px-1.5 py-0.5 text-[11px] font-medium tabular-nums text-muted-foreground">
                                          {groupModels.length} 个模型
                                        </span>
                                      </div>
                                      {/* 清单：限高内滚（不把弹层顶出视口），行间细分隔线 */}
                                      <ul className="max-h-64 divide-y divide-border/60 overflow-y-auto">
                                        {groupModels.map((model) => (
                                          <li
                                            key={model.id}
                                            data-testid={`group-${group.key}-model`}
                                            className="flex items-baseline justify-between gap-3 px-3 py-1"
                                          >
                                            <span className="min-w-0 truncate text-xs text-foreground">
                                              {model.label}
                                            </span>
                                            {/* 倍率做成数值胶囊：等宽数字 + 免费额度单独着色，
                                                扫一列倍率时数字右对齐、颜色分档一眼可辨 */}
                                            <span
                                              className={cn(
                                                "shrink-0 rounded px-1 text-[11px] font-semibold tabular-nums",
                                                model.rate === undefined
                                                  ? "text-muted-foreground"
                                                  : model.rate === 0
                                                    ? "bg-ok/12 text-ok-ink"
                                                    : "bg-muted text-foreground",
                                              )}
                                            >
                                              {model.rate === undefined ? "—" : rateLabel(model.rate)}
                                            </span>
                                          </li>
                                        ))}
                                      </ul>
                                      {/* 脚注：口径说明，避免「免 / —」被误读 */}
                                      <div className="border-t border-border px-3 py-1.5 text-[11px] leading-snug text-muted-foreground">
                                        倍率＝该渠道的消耗倍率；<span className="text-ok-ink">免</span>＝免费额度，
                                        — ＝上游未给倍率
                                      </div>
                                    </div>
                                  }
                                >
                                  <button
                                    type="button"
                                    className="flex w-40 shrink-0 cursor-help items-center gap-2 rounded-md py-2 pr-3 pl-1 text-left focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
                                    data-testid={`group-${group.key}-model-count`}
                                  >
                                    <span className="grid size-7 shrink-0 place-items-center rounded-md bg-background text-muted-foreground ring-1 ring-border/70">
                                      <Sparkles className="size-3.5" aria-hidden />
                                    </span>
                                    <span className="min-w-0">
                                      <span className="block text-[11px] leading-tight text-muted-foreground">模型</span>
                                      <span className="block truncate text-xs leading-tight font-medium tabular-nums text-foreground">
                                        {groupModels.length} 个
                                        {rateText !== null && (
                                          <span className="ml-1 font-normal text-muted-foreground">
                                            · {rateText}
                                          </span>
                                        )}
                                      </span>
                                    </span>
                                  </button>
                                </LongTextTip>
                              )}
                            </span>
                            </div>
                          </TableCell>
                        </TableRow>
  );
}
