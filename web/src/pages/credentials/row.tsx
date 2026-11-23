import {
  ArrowDownIcon,
  CalendarCheck,
  Cookie,
  Download,
  Gauge,
  HeartPulse,
  History,
  KeyRound,
  MoreHorizontal,
  Pin,
  Power,
  Radio,
  RefreshCw,
  Sparkles,
  Tag,
  Trash2,
} from "lucide-react";
import {
  cooldownRemaining,
  credentialState,
  expiringQuotaLabel,
  formatDuration,
  formatNumber,
  formatTime,
  hasQuotaProbe,
  healthView,
  QUOTA_UNIT,
  quotaSemantics,
  STATE_LABEL,
  STATE_TONE,
  tokenExpiryView,
} from "../../api/display";
import type { Credential } from "../../api/types";
import { editMeta } from "../../api/credentials";
import type { Ledger } from "../../lib/credentialList";
import { ColumnHint, LongTextTip } from "../../components/Tip";
import { SortableHead } from "../../components/SortableHead";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "../../components/ui/dropdown-menu";
import { Badge, Button, Checkbox, TableCell, TableHead, TableRow } from "../../ui";
import type { Actions } from "./actions";
import { ModelCooldownList, PackageLadder, TokenExpiry } from "./cells";
import { supportsActivity, supportsCheckin, supportsGrowth } from "./helpers";

/**
 * 列头行：**迁移到每个展开的渠道组内部**，不再是全局表头。
 *
 * 折叠态下组头本身就是一张渠道仪表盘，列名没有对应内容；只有展开某组时才需要
 * 「这一列是什么」的锚点。所以列头跟着展开区走，并在滚动容器里吸顶（`[&>th]:sticky`）。
 *
 * `[&>th]:bg-muted` 必须不透明：基类给 tr 设的是 `bg-muted/40`，滚动时行内容
 * 会从半透明的表头底色里透出来。`testId` 带渠道前缀——多个组同时展开时，
 * 同一列会渲染多份，不带前缀会撞 id。
 */
export function ColumnHeaderRow({
  groupKey,
  canWrite,
  showGrowth,
  sort,
  order,
  toggle,
}: {
  groupKey: string;
  canWrite: boolean;
  showGrowth: boolean;
  sort: string;
  order: "asc" | "desc";
  toggle: (columnKey: string) => void;
}) {
  return (
    <TableRow
      data-testid={`column-header-${groupKey}`}
      className="hover:bg-transparent [&>th]:sticky [&>th]:top-0 [&>th]:z-10 [&>th]:h-9 [&>th]:bg-muted"
    >
      {/* 选择列：列头留空（每行的 checkbox 由 Row 渲染，组头的全选在组标题行）。
          必须与 Row 的 checkbox 格一一对应，否则表格 auto 布局会整列错位。 */}
      {canWrite && <TableHead className="w-8" aria-hidden />}
      <SortableHead label="凭证" columnKey="nickname" active={sort === "nickname"}
                    direction={order} onToggle={toggle} testId={`sort-${groupKey}-nickname`}
                    hint={<ColumnHint text="昵称；「已指定」＝调度器优先用该凭证（全局唯一）。渠道名与数量在分组标题行。" />} />
      <SortableHead label="状态 / 健康度" columnKey="health" active={sort === "health"}
                    direction={order} onToggle={toggle} testId={`sort-${groupKey}-health`}
                    hint={<ColumnHint text="状态：可用/冷却中/已禁用/已暂停/额度耗尽。已暂停只摘对话流量，签到/刷新/探测照常；冷却中到期恢复。健康度四态：已知百分比/未探测/无探测/已耗尽——未探测＝探测失败或渠道未给额度（可重试）≠已耗尽；无探测＝免费层无额度接口。调度器优先百分比高者，打平比积分。" />} />
      <SortableHead label="额度" columnKey="quota_remaining"
                    active={sort === "quota_remaining"} direction={order}
                    onToggle={toggle} testId={`sort-${groupKey}-quota_remaining`}
                    hint={<ColumnHint text="剩余 / 总量。周期口径按渠道（CodeBuddy 本周期剩余、TRAE 账户剩余递减、CodeArts 积分余额、zen/kilo 免费层无额度接口）；悬浮额度看口径。下箭头展开本组的变动流水。" />} />
      <SortableHead label="token 剩余" columnKey="token_expires_at"
                    active={sort === "token_expires_at"} direction={order}
                    onToggle={toggle} testId={`sort-${groupKey}-token_expires_at`}
                    hint={<ColumnHint text="access token 距到期还有多久，取自凭证本身（0 表示渠道未给到期信息，显示 —）。预刷新任务每小时检查，进入 24 小时窗口自动续期；「已过期」＝上游会拒绝，需重登。" />} />
      {/* 成长中心列只在支持的渠道出现：其余渠道后端没有 growth 能力，常驻一列只会整列「—」。
          **但列数必须恒定**：表格是 auto 布局，少一格会重算整张表的列宽，下面展开的组
          就会整列错位（操作列从 1207 跳到 926）。故不支持的渠道用一个空占位格顶住，
          而不是让操作列 colSpan 吃掉——占位格不参与内容定宽，列宽口径才不变。 */}
      {showGrowth ? (
        <SortableHead label="成长中心" columnKey="growth_last_run_at"
                      active={sort === "growth_last_run_at"} direction={order}
                      onToggle={toggle} testId={`sort-${groupKey}-growth_last_run_at`}
                      hint={<ColumnHint text="仅 CodeBuddy：最近一轮成长中心（旅行礼物/任务/连登兑换/盲盒）的领取结果与时间，由定时任务或手动执行写入。汇报较长时截断，悬浮看全文。" />} />
      ) : (
        <TableHead aria-hidden />
      )}
      {canWrite && (
        <TableHead className="text-right"><span className="inline-flex items-center gap-1">操作<ColumnHint text="探测/签到：行内常驻。更多操作（⋯）：成长中心（仅 CodeBuddy）、活跃上报（CodeBuddy 与 CodeBuddy 国际）、指定、暂停/恢复、删除。变动流水在额度列下箭头。" /></span></TableHead>
      )}
    </TableRow>
  );
}

export function Row({
  credential,
  ledger,
  now,
  expiryWindow,
  expirySecondaryWindow,
  tokenWarning,
  canWrite,
  showGrowth,
  busy,
  selected,
  onSelect,
  confirming,
  creditsOpen,
  onConfirmDelete,
  actions,
}: {
  credential: Credential;
  /** 本行讲哪一本账：credits＝CodeArts 积分，pool＝其余渠道的套餐/每日池余额 */
  ledger: Ledger;
  now: number;
  expiryWindow: number;
  expirySecondaryWindow: number;
  tokenWarning: number;
  canWrite: boolean;
  showGrowth: boolean;
  busy: boolean;
  selected: boolean;
  onSelect: (credentialId: string, checked: boolean) => void;
  confirming: boolean;
  creditsOpen: boolean;
  onConfirmDelete: (id: string | null) => void;
  actions: Actions;
}) {
  // 本行讲哪本账由渠道决定（`providerLedger`）：数值、口径说明、流水入口都按
  // 它取——两本账在上游是两套接口、量级差一个数量级，混在一行里会被读成
  // 同一本账的两次读数。CodeArts 的额度来自积分接口，其余渠道是套餐口径。
  const creditsLedger = ledger === "credits";
  const remaining = credential.quota_remaining;
  const total = credential.quota_total;
  // 到期积分：与额度同源（后端按凭证自己的阶梯算好）。
  const expiring = expiringQuotaLabel(
    credential.quota_expiring_credits, expiryWindow, "primary", QUOTA_UNIT);
  // 次窗口只在**比主窗口多**时才渲染：7 天窗口是 36h 的超集，两块数字相同
  // 说明没有「超出主窗口的部分」，再列一行只是把同一个数字说两遍。它的作用是
  // 解释「36h 内没有到期积分、但一周内会过期」的账号为何仍被优先选中。
  const expiringSecondary = !expiring
    ? expiringQuotaLabel(
        credential.quota_expiring_credits_secondary,
        expirySecondaryWindow,
        "secondary",
        QUOTA_UNIT,
      )
    : null;
  const health = healthView(credential.health, credential.provider);
  const state = credentialState(credential, now);
  const cooldown = cooldownRemaining(credential.cooling_until, now);
  const tokenExpiry = tokenExpiryView(
    credential.token_expires_at, tokenWarning, now, credential.token_issued_at);

  // 抽屉由调用方渲染成**独立的下一行**（见 TableBody）：塞进本行的最后一个单元格
  // 只会挤在「操作」列里——同行内的 colSpan 不生效，整行高度会被拉坏。
  return (
    <TableRow data-testid={`row-${credential.id}`}>
      {/* 选择格：与 ColumnHeaderRow 的空表头一一对应（列数恒定）。
          checkbox 只负责勾选，不触发任何行级动作——误点空白处不应产生副作用。 */}
      {canWrite && (
        <TableCell label="选择" className="w-8">
          <Checkbox
            checked={selected}
            disabled={busy}
            aria-label={`选择 ${credential.nickname || credential.id.slice(0, 12)}`}
            data-testid={`select-${credential.id}`}
            onCheckedChange={(checked) => onSelect(credential.id, checked === true)}
          />
        </TableCell>
      )}
      <TableCell label="凭证">
        {/* 渠道名不在这里重复：它已在组头（按渠道分组后，同一组内所有行的渠道名
            完全相同）。昵称单独占一行，「已指定」紧贴昵称。 */}
        <div className="flex min-w-0 items-center gap-2">
          <span
            className="truncate font-medium"
            title={credential.nickname || credential.id.slice(0, 12)}
          >
            {credential.nickname || credential.id.slice(0, 12)}
          </span>
          {credential.pinned === 1 && <Badge tone="accent">已指定</Badge>}
          {credential.limits && (
            <span data-testid={`limits-badge-${credential.id}`}>
              <Badge tone="muted">限额</Badge>
            </span>
          )}
          {/* 账号级模型白名单（v39）：有值时给个徽章，否则「这个号为什么没被
              选中」在列表上完全看不出来。加载态由 editMeta 兜默认值（无则空串）。 */}
          {editMeta(credential).allowed_models !== "" && (
            <span
              data-testid={`models-badge-${credential.id}`}
              title={`模型白名单：${editMeta(credential).allowed_models}`}
            >
              <Badge tone="muted">限模型</Badge>
            </span>
          )}
          {/* 标签紧贴昵称（它是这一行的记号）：最多显示两个，多余的收成 +N 并
              在 title 里给全量，避免长标签把昵称挤到省略号。 */}
          {credential.tags.length > 0 && (
            <span
              className="flex shrink-0 items-center gap-1"
              data-testid={`tags-${credential.id}`}
              title={credential.tags.join(" / ")}
            >
              {credential.tags.slice(0, 2).map((tag) => (
                <Badge key={tag} tone="muted">{tag}</Badge>
              ))}
              {credential.tags.length > 2 && (
                <span className="text-xs text-muted-foreground">
                  +{credential.tags.length - 2}
                </span>
              )}
            </span>
          )}
        </div>
      </TableCell>
      <TableCell label="状态 / 健康度">
        {/* 状态 + 健康度合并成一列：两个 Badge 同行，附加说明换行小字 */}
        <div className="space-y-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge tone={STATE_TONE[state]}>{STATE_LABEL[state]}</Badge>
            <Badge tone={health.tone}>{health.label}</Badge>
          </div>
          {state === "cooling" && (
            <div className="text-xs text-muted-foreground">{formatDuration(cooldown)}</div>
          )}
          {credential.disabled_reason && state === "disabled" && (
            <div className="text-xs text-muted-foreground">
              {credential.disabled_reason}
            </div>
          )}
        </div>
      </TableCell>
      <TableCell label="额度" className="text-xs">
        <div className="flex flex-wrap items-baseline gap-x-2">
          {/* 数字与流水入口同处一个 inline-flex：按钮跟数字做行内居中对齐，
              而不是与整行 baseline 对齐（图标按钮没有文字基线，会明显偏低）。 */}
          <span className="inline-flex items-center gap-1">
            <span
              // 周期语义（本周期剩余 / 账户剩余 / 积分余额 / 重置日期）
              // 从独立的第三行收进数字的悬浮说明：同一渠道所有行的这句话几乎
              // 完全一样，逐行复读只是把行高撑高一倍。虚线下划线提示可悬浮。
              className="cursor-help underline decoration-dotted underline-offset-2"
              title={quotaSemantics(credential)}
            >
              {formatNumber(remaining)} / {formatNumber(total)}
            </span>
            {/* 流水入口紧贴它要解释的数字：曾经在「更多操作」菜单里，打开前看不到
                任何余额上下文。下箭头是行内展开语义（非弹层），展开后箭头翻转。
                仅 admin/operator 可用——接口本身就是该门槛。zen / kilo 免费层没有
                额度接口、也没有额度探测，流水恒为空，入口只会给出空抽屉，不渲染。
                `ledger` 按本行讲的那本账传（CodeArts＝积分账），否则会读到另一本账。 */}
            {canWrite && hasQuotaProbe(credential.provider) && (
              <Button
                // 项目封装的 Button：variant="default" 即 shadcn 的 outline
                variant="default"
                size="icon"
                aria-label="变动流水"
                aria-expanded={creditsOpen}
                title={creditsLedger
                  ? "积分流水：积分账两次探测的净变化"
                  : "额度流水：额度两次探测的净变化"}
                data-testid={`credits-${credential.id}`}
                onClick={() => actions.credits(credential, ledger)}
                // 与数字同处一个 inline-flex items-center：图标按钮没有文字基线，
                // 靠 baseline 对齐会明显偏低。不要再加 translate 微调——按钮
                // 中心与数字墨迹中心实测只差 0.26px（12px 字号），属亚像素。
                className={`size-4 shrink-0 ${
                  creditsOpen ? "text-foreground" : "text-muted-foreground"
                }`}
              >
                <ArrowDownIcon
                  className={`size-2 transition-transform ${creditsOpen ? "rotate-180" : ""}`}
                />
              </Button>
            )}
            <ModelCooldownList credential={credential} now={now} />
          </span>
          {/* 套餐明细：CodeBuddy / TRAE 的权益包与 CodeArts 的积分包都走这里。 */}
          {<PackageLadder credential={credential} />}
        </div>
        {/* 到期积分：直接来自后端按凭证自己的阶梯算好的窗口内到期额。 */}
        {expiring && (
          <div className="text-warn" data-testid="quota-expiring">
            {expiring}
          </div>
        )}
        {expiringSecondary && (
          <div className="text-warn-ink" data-testid="quota-expiring-secondary">
            {expiringSecondary}
          </div>
        )}
      </TableCell>
      <TableCell label="token 剩余" className="text-xs">
        <TokenExpiry credential={credential} view={tokenExpiry} />
      </TableCell>
      {showGrowth && (
        <TableCell label="成长中心" className="text-xs text-muted-foreground">
          {credential.growth_last_result ? (
            // 汇报可能很长（多步领取串成一行）：限宽单行截断，hover 看全文
            <LongTextTip content={credential.growth_last_result}>
              <div data-testid={`growth-${credential.id}`} className="max-w-[22rem]">
                <div className="truncate">{credential.growth_last_result}</div>
                <div className="text-muted-foreground">
                  {formatTime(credential.growth_last_run_at)}
                </div>
              </div>
            </LongTextTip>
          ) : (
            <span>—</span>
          )}
        </TableCell>
      )}
      {/* 同 ColumnHeaderRow：不支持的渠道用一个空占位格顶住成长中心那一列，
          列数恒定，操作列才落在同一列上（表格是 auto 布局）。 */}
      {!showGrowth && <TableCell aria-hidden />}
      {canWrite && (
        <TableCell label="操作">
          <div className="flex items-center justify-end gap-1">
            {confirming ? (
              <>
                <Button
                  size="sm"
                  variant="danger"
                  disabled={busy}
                  onClick={() => actions.remove(credential)}
                >
                  确认删除
                </Button>
                <Button size="sm" variant="ghost" onClick={() => onConfirmDelete(null)}>
                  取消
                </Button>
              </>
            ) : (
              <>
                {/* 探测 / 签到是高频日常动作，从「更多」菜单提出来常驻，
                    少一次点击；其余低频动作仍收在菜单里。
                    zen / kilo 免费层探测无意义（上游无额度接口，探测恒失败）；
                    签到现全渠道支持（CodeArts 已接入）。 */}
                {hasQuotaProbe(credential.provider) && (
                  <>
                    <Button
                      size="sm"
                      variant="default"
                      disabled={busy}
                      data-testid={`probe-${credential.id}`}
                      onClick={() => actions.probe(credential)}
                    >
                      <RefreshCw className="size-3.5" /> 探测
                    </Button>
                    {supportsCheckin(credential.provider) && (
                      <Button
                        size="sm"
                        variant="default"
                        disabled={busy}
                        data-testid={`checkin-${credential.id}`}
                        onClick={() => actions.checkin(credential)}
                      >
                        <CalendarCheck className="size-3.5" /> 签到
                      </Button>
                    )}
                  </>
                )}
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button
                      size="icon"
                      variant="ghost"
                      aria-label="更多操作"
                      disabled={busy}
                      data-testid={`actions-${credential.id}`}
                    >
                      <MoreHorizontal />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end" className="w-44">
                    {supportsGrowth(credential.provider) && (
                      <DropdownMenuItem
                        onSelect={() => actions.growth(credential)}
                        title="手动跑一轮成长中心领取（与定时任务同一路径）"
                      >
                        <Sparkles className="size-4" /> 成长中心
                      </DropdownMenuItem>
                    )}
                    {supportsActivity(credential.provider) && (
                      <DropdownMenuItem onSelect={() => actions.activity(credential)}>
                        <Radio className="size-4" /> 活跃上报
                      </DropdownMenuItem>
                    )}
                    {supportsCheckin(credential.provider) && (
                      <DropdownMenuItem
                        onSelect={() => actions.checkinLog(credential)}
                        title="签到审计日志：最近几次签到的时间/结果/领取积分/上游业务码"
                      >
                        <History className="size-4" /> 签到记录
                      </DropdownMenuItem>
                    )}
                    <DropdownMenuSeparator />
                    {credential.disabled === 1 && (
                      <DropdownMenuItem onSelect={() => actions.revive(credential)}>
                        <HeartPulse className="size-4" /> 恢复
                      </DropdownMenuItem>
                    )}
                    {credential.disabled === 1 && (
                      <DropdownMenuItem
                        onSelect={() => actions.forceRevive(credential)}
                        title="连上游点名的冷却（限流/余额/权益/风控）一起清除；清除后可能立刻再撞一次上游"
                      >
                        <HeartPulse className="size-4" /> 强制恢复
                      </DropdownMenuItem>
                    )}
                    <DropdownMenuItem onSelect={() => actions.toggle(credential)}>
                      {/* 「取消暂停」而非「恢复」：菜单里 disabled=1 那条已叫「恢复」
                          （revive，解除 session 死亡硬禁用），两者撞名会误操作 */}
                      <Power className="size-4" /> {credential.enabled === 1 ? "暂停" : "取消暂停"}
                    </DropdownMenuItem>
                    <DropdownMenuItem onSelect={() => actions.pin(credential)}>
                      <Pin className="size-4" /> {credential.pinned === 1 ? "取消指定" : "指定"}
                    </DropdownMenuItem>
                    <DropdownMenuItem onSelect={() => actions.limits(credential)}>
                      <Gauge className="size-4" />
                      {credential.limits || editMeta(credential).allowed_models
                        ? "编辑限额/模型"
                        : "设置限额/模型"}
                    </DropdownMenuItem>
                    <DropdownMenuItem onSelect={() => actions.tags(credential)}>
                      <Tag className="size-4" />
                      {credential.tags.length ? "编辑标签" : "设置标签"}
                    </DropdownMenuItem>
                    <DropdownMenuItem
                      onSelect={() => actions.weight(credential)}
                      title="只调整同健康档内的选号次序：正数越优先、负数当备用，不会把不可用的号顶上来"
                    >
                      <Gauge className="size-4" />
                      {credential.routing_weight
                        ? `权重 ${credential.routing_weight}`
                        : "设置权重"}
                    </DropdownMenuItem>
                    {credential.provider === "raccoon" && (
                      <DropdownMenuItem
                        onSelect={() => actions.loginSecret(credential)}
                        title="保存网页端账号密码，用于「按需登录」换新会话（上游只在登录时补发当天每日积分）"
                      >
                        <KeyRound className="size-4" /> 账号密码
                        {credential.login_secret === 1 && (
                          <span className="text-muted-foreground">（已保存）</span>
                        )}
                      </DropdownMenuItem>
                    )}
                    {["qoder", "codearts"].includes(credential.provider) && (
                      <DropdownMenuItem
                        onSelect={() => actions.webCookie(credential)}
                        title="写入网页 Cookie 后额度探测改用网页接口，可拿到每个积分包的独立到期日"
                      >
                        <Cookie className="size-4" /> 网页 Cookie
                        {credential.web_cookie_state === "expired" && (
                          <span className="text-destructive">（已失效）</span>
                        )}
                      </DropdownMenuItem>
                    )}
                    <DropdownMenuItem
                      onSelect={() => actions.export(credential)}
                      title="导出为 JSON（含明文令牌，可原样再导入；操作会记审计）"
                    >
                      <Download className="size-4" /> 导出凭证
                    </DropdownMenuItem>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem
                      onSelect={() => onConfirmDelete(credential.id)}
                      className="text-destructive focus:text-destructive focus:bg-destructive/10"
                    >
                      <Trash2 className="size-4" /> 删除
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              </>
            )}
          </div>
        </TableCell>
      )}
    </TableRow>
  );
}
