import { Fragment, useState } from "react";
import type { ReactNode } from "react";
import {
  ArrowDownIcon,
  CalendarCheck,
  Database,
  HeartPulse,
  MoreHorizontal,
  Pin,
  Plus,
  Power,
  Radio,
  RefreshCw,
  Sparkles,
  Trash2,
  TriangleAlert,
} from "lucide-react";
import { api } from "../api/client";
import { useSessionContext } from "../Layout";
import { useCredentials, useQueryClient } from "../api/hooks";
import { PoolOverview } from "../components/PoolOverview";
import { AddCredentialDialog } from "../components/AddCredentialDialog";
import { ToastViewport, useToasts } from "../components/Toast";
import { ColumnHint, LongTextTip } from "../components/Tip";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { ProviderIcon } from "../components/ProviderIcon";
import { SortableHead } from "../components/SortableHead";
import { useSort } from "../hooks/useSort";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  activeModelCooldowns,
  CREDIT_SOURCE_LABEL,
  creditEventLabel,
  credentialState,
  cooldownRemaining,
  expiringQuotaLabel,
  formatDuration,
  formatNumber,
  formatTime,
  hasQuotaProbe,
  healthView,
  modelCooldownLabel,
  probeFailureLabel,
  QUOTA_UNIT,
  quotaSemantics,
  STATE_LABEL,
  STATE_TONE,
  tokenExpiryView,
} from "../api/display";
import type { Credential, CreditEvent, GrowthRunResult } from "../api/types";
import { PROVIDER_LABEL } from "../api/providers";
import type { TokenExpiryView } from "../api/display";
import {
  Badge,
  Button,
  Card,
  Empty,
  EmptyState,
  Notice,
  Panel,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../ui";

/**
 * 渠道是否支持每日签到。
 *
 * 目前全部渠道都有签到：CodeArts 自 Q72 起接入 `/v1/ops/delivery`
 * （此前「上游没有签到接口」的结论已推翻）。保留函数是为了将来某个渠道
 * 下线签到时不至于把按钮散落在 JSX 里，也便于测试单点断言。
 */
function supportsCheckin(_provider: Credential["provider"]): boolean {
  return true;
}

/** 套餐到期日：只要日期，时分秒对"哪个包先过期"没用。 */
function packageExpiry(epoch: number | null): string {
  return epoch === null ? "—" : new Date(epoch * 1000).toLocaleDateString("zh-CN");
}

/** 成长中心步骤状态的人话标签；idle 不是失败（无事可做），必须与 failed 分开显示。 */
const GROWTH_STEP_LABEL: Record<GrowthRunResult["steps"][number]["status"], string> = {
  done: "已领取",
  idle: "未执行",
  skipped: "已关闭",
  failed: "失败",
};

const GROWTH_STEP_TONE: Record<GrowthRunResult["steps"][number]["status"], string> = {
  done: "text-ok",
  idle: "text-muted-foreground",
  skipped: "text-muted-foreground",
  failed: "text-danger",
};

/** 一句话汇报：登录失效要单独提示「重新登录」，不能与普通失败混同。 */
function growthNotice(result: GrowthRunResult): string {
  if (result.session_dead) return "登录态已失效，请重新登录该渠道";
  return result.report || (result.ok ? "成长中心执行完成" : "成长中心执行未完全成功");
}

/** 成长中心浮层内容：一句话汇报 + 逐步结果。步骤是结构化清单，信息量大，
 * 因此给它比普通提示更长的停留时间，并在浮层内保留完整清单（idle 不是失败，
 * 必须与 failed 分开显示）。 */
function GrowthToastContent({ result }: { result: GrowthRunResult }) {
  return (
    <div className="space-y-1">
      <div className="font-medium">成长中心：{growthNotice(result)}</div>
      <ul className="space-y-0.5 text-xs text-muted-foreground">
        {result.steps.map((step, index) => (
          <li key={`${step.name}-${index}`} data-testid="growth-step">
            <span className={GROWTH_STEP_TONE[step.status]}>
              {GROWTH_STEP_LABEL[step.status]}
            </span>
            {step.name}
            {step.detail && `：${step.detail}`}
          </li>
        ))}
      </ul>
      {result.session_dead && (
        <div className="text-xs">登录态已失效，需重新登录该渠道后才能继续领取。</div>
      )}
    </div>
  );
}

interface Actions {
  revive: (credential: Credential) => void;
  toggle: (credential: Credential) => void;
  pin: (credential: Credential) => void;
  remove: (credential: Credential) => void;
  probe: (credential: Credential) => void;
  checkin: (credential: Credential) => void;
  growth: (credential: Credential) => void;
  activity: (credential: Credential) => void;
  credits: (credential: Credential) => void;
}

export function CredentialsPage() {
  const session = useSessionContext();
  // 默认 created_at 升序（与后端一致）；额度 / 健康度等数值列首点降序
  const { sort, order, toggle } = useSort("created_at", "asc",
    { health: "desc", quota_remaining: "desc", token_expires_at: "asc",
      growth_last_run_at: "desc" });
  // 30s 轮询：冷却倒计时 / token 剩余都是「随时间变化」的观测量，页面停留
  // 时应自动刷新（与 useTasks 同口径）。
  const { data, isLoading } = useCredentials(session.username, 30_000, sort, order);
  const client = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  // 添加凭证对话框（登录渠道 / JSON 导入 / 免费层补回都收在里面）
  const [addOpen, setAddOpen] = useState(false);
  // 操作结果统一走顶部居中浮层（toast），不再以插入式横幅撑开页面：
  // error 与 notice 各占一个槽位，同类提示只保留最新一条。
  const { toasts, push, dismiss } = useToasts();
  const showError = (content: ReactNode) => push({ tone: "danger", testId: "credentials-error", content });
  const showNotice = (content: ReactNode) => push({ tone: "ok", testId: "credentials-notice", content });
  // 积分记录抽屉：一次只开一个（同一行内展开，不弹层——
  // 表格行本身就是最好的上下文，弹层会遮掉额度列）
  const [creditEvents, setCreditEvents] = useState<{
    credentialId: string;
    events: CreditEvent[];
    sort: string;
    order: string;
  } | null>(null);

  const credentials = data?.credentials ?? [];
  // OpenCode Zen 无凭证：池里只有一条虚拟占位行，正常由服务启动时种子。
  // 用户在凭证页删除后不会自动回来，这里给一个一键补回的入口（收在
  // 「添加凭证」对话框里），免得必须重启服务（见 README「OpenCode Zen 免费层」）。
  const hasZen = credentials.some((item) => item.provider === "zen");
  // Kilo Gateway 同为无凭证免费层（同 zen）：删除虚拟占位行后也留一键补回。
  const hasKilo = credentials.some((item) => item.provider === "kilo");
  const expiryWindow = data?.expiry_window_seconds ?? 0;
  const expirySecondaryWindow = data?.expiry_secondary_window_seconds ?? 0;
  const tokenWarning = data?.token_expiry_warning_seconds ?? 0;
  // 写门槛是 admin 或 operator（与后端 require_operator 同口径）；viewer 只读。
  const canWrite = data?.is_operator ?? false;
  const now = Date.now() / 1000;

  const refresh = () => client.invalidateQueries({ queryKey: ["admin"] });

  // 积分记录抽屉内排序：改排序需重新取数（后端排），并保持方向直觉
  // （时间/数值列首点降序，来源首点升序）。
  const CREDIT_FIRST_ORDER: Record<string, "asc" | "desc"> = {
    ts: "desc", window_start: "desc", delta: "desc", source: "asc",
  };
  const sortCredits = async (columnKey: string) => {
    if (!creditEvents) return;
    const order = creditEvents.sort === columnKey
      ? (creditEvents.order === "asc" ? "desc" : "asc")
      : (CREDIT_FIRST_ORDER[columnKey] ?? "asc");
    try {
      const result = await api.creditEvents(creditEvents.credentialId, columnKey, order);
      setCreditEvents({ ...creditEvents, events: result.events, sort: columnKey, order });
    } catch (caught) {
      showError(caught instanceof Error ? caught.message : "积分记录读取失败");
    }
  };

  const run = async (task: () => Promise<unknown>, successMessage?: string) => {
    setBusy(true);
    try {
      await task();
      if (successMessage) showNotice(successMessage);
      await refresh();
    } catch (caught) {
      showError(caught instanceof Error ? caught.message : "操作失败");
    } finally {
      setBusy(false);
    }
  };

  const actions: Actions = {
    // 会话失效被硬禁用的凭证：重新登录后用「恢复」放回池子，
    // 不必删除重建（以前只能删除）。
    revive: (credential) =>
      void run(() => api.reviveCredential(credential.id), "已恢复该凭证，可重新参与调度。"),
    toggle: (credential) =>
      void run(
        () => api.toggleCredential(credential.id, credential.enabled !== 1),
        // 文案与「已暂停」状态一致：只摘对话流量，后台任务不受影响
        credential.enabled === 1
          ? "已暂停该凭证的对话流量；签到 / 刷新 / 探测照常运行。"
          : "已恢复该凭证的对话流量。",
      ),
    pin: (credential) =>
      void run(
        () => api.pinCredential(credential.pinned === 1 ? null : credential.id),
        credential.pinned === 1 ? "已取消指定。" : "已指定为优先使用。",
      ),
    remove: (credential) =>
      void run(() => api.deleteCredential(credential.id), "凭证已删除。").then(() =>
        setConfirmDelete(null),
      ),
    probe: (credential) =>
      void (async () => {
        setBusy(true);
        try {
          const result = await api.probeCredential(credential.id);
          // 探测失败表示「未知」，绝不能显示成额度为 0。
          // 失败原因用可操作的中文说明，不直接把后端枚举或异常类名丢给用户。
          if (result.probed) {
            showNotice(
              `探测成功：剩余 ${formatNumber(result.remaining)} / ${formatNumber(result.total)}`,
            );
          } else {
            showNotice(
              <>
                {`探测失败：${probeFailureLabel(result.reason)}（健康度保持为「未探测」）`}
                {result.detail && (
                  <span className="mt-1 block text-xs text-muted-foreground" data-testid="probe-detail">
                    原始错误：{result.detail}
                  </span>
                )}
              </>,
            );
          }
          await refresh();
        } catch (caught) {
          showError(caught instanceof Error ? caught.message : "探测失败");
        } finally {
          setBusy(false);
        }
      })(),
    checkin: (credential) =>
      void (async () => {
        setBusy(true);
        try {
          const result = await api.checkinCredential(credential.id);
          // 渠道可选回填的活动状态：连续天数/今日积分（TRAE 为 null，拼接自动跳过）
          const streak = result.status?.streak_days ?? null;
          const tail = streak === null ? "" : `（连续 ${streak} 天）`;
          if (result.ok && result.already_checked_in) {
            // 渠道把「已签到」返回成 HTTP 400 + code=10001，这不是错误
            showNotice(`${result.message || "今天已签到，请明天再来"}${tail}`);
          } else if (result.ok) {
            showNotice(
              (result.credit === null
                ? "签到成功"
                : `签到成功，获得 ${formatNumber(result.credit)} 积分`) + tail,
            );
          } else {
            showNotice(
              `签到未成功（code=${result.code ?? "null"}）${result.message ? ` ${result.message}` : ""}`,
            );
          }
          await refresh();
        } catch (caught) {
          showError(caught instanceof Error ? caught.message : "签到失败");
        } finally {
          setBusy(false);
        }
      })(),
    growth: (credential) =>
      void (async () => {
        setBusy(true);
        try {
          const result = await api.runGrowth(credential.id);
          // 成长中心是多步清单，给更长的停留时间；失败用 danger 让读屏强播报。
          push({
            tone: result.ok ? "ok" : "danger",
            testId: "growth-result",
            duration: 12_000,
            content: <GrowthToastContent result={result} />,
          });
          await refresh();
        } catch (caught) {
          showError(caught instanceof Error ? caught.message : "成长中心执行失败");
        } finally {
          setBusy(false);
        }
      })(),
    // 只读：开关抽屉不写任何状态，因此不复用 run()（那个会刷新列表）
    credits: (credential) =>
      void (async () => {
        if (creditEvents?.credentialId === credential.id) {
          setCreditEvents(null);
          return;
        }
        try {
          const result = await api.creditEvents(credential.id);
          setCreditEvents({
            credentialId: credential.id, events: result.events,
            sort: "ts", order: "desc",
          });
        } catch (caught) {
          showError(caught instanceof Error ? caught.message : "积分记录读取失败");
        }
      })(),
    activity: (credential) =>
      void (async () => {
        setBusy(true);
        try {
          const result = await api.reportActivity(credential.id);
          showNotice(result.ok
            ? "活跃上报成功（已补发一条对话事件）"
            : `活跃上报失败：${result.message || "未知原因"}`);
          await refresh();
        } catch (caught) {
          showError(caught instanceof Error ? caught.message : "活跃上报失败");
        } finally {
          setBusy(false);
        }
      })(),
  };

  if (isLoading) return <PageSkeleton variant="cards" />;

  return (
    <div className="space-y-6" data-testid="credentials-page">
      <PageHeader
        eyebrow="控制台"
        title="凭证管理"
        description="凭证是调度池里可被选中的渠道账号。登录渠道授权或粘贴 JSON 导入后，可在此探测剩余额度、签到或启停；顶部池概况按健康度四态说明这个池现在还剩多少能打的号。"
        icon={<Database className="size-5" />}
      />
      {/* 池概览（原「池仪表盘」页内容合并于此）：统计卡 + 健康度四态分布 */}
      <PoolOverview credentials={credentials} now={now} />
      {!canWrite && (
        <div data-testid="readonly-banner">
          <Notice tone="muted">只读模式：仅管理员与操作员可以导入、启停或删除凭证。</Notice>
        </div>
      )}
      {credentials.length === 0 && canWrite && (
        <div data-testid="first-run-hint">
          <Notice tone="muted">
            还没有凭证。点凭证池右上角「添加凭证」登录渠道账号（CodeBuddy / TRAE /
            Qoder / CodeArts），或直接粘贴凭证 JSON 导入；OpenCode Zen / Kilo
            Gateway 免费层在对话框里一键添加即可。
          </Notice>
        </div>
      )}
      {/* 操作结果（探测/签到/导入/成长中心等）统一在顶部居中浮层播报，
          不再以插入式横幅撑开页面；浮层自带 role=alert/status 供读屏播报。 */}
      <ToastViewport toasts={toasts} onDismiss={dismiss} />

      <Panel
        title="凭证池"
        action={canWrite && (
          <Button
            size="sm"
            variant="primary"
            data-testid="open-add-dialog"
            disabled={busy}
            onClick={() => setAddOpen(true)}
          >
            <Plus className="size-4" /> 添加凭证
          </Button>
        )}
      >
        {credentials.length === 0 ? (
          <EmptyState
            icon={<Database className="size-5" />}
            title="还没有凭证"
            description="用右上角「添加凭证」登录渠道账号或导入凭证 JSON，即可让调度池开始工作。"
            data-testid="no-credentials"
          />
        ) : (
          <Table data-testid="credentials-table">
            <TableHeader>
              <TableRow>
                <SortableHead label="凭证" columnKey="nickname" active={sort === "nickname"}
                              direction={order} onToggle={toggle} testId="sort-nickname"
                              hint={<ColumnHint text="昵称 + 所属渠道；「已指定」表示调度器优先使用该凭证（全局唯一）。" />} />
                <SortableHead label="状态 / 健康度" columnKey="health" active={sort === "health"}
                              direction={order} onToggle={toggle} testId="sort-health"
                              hint={<ColumnHint text="状态：可用/冷却中/已禁用/已暂停/额度耗尽；已暂停只摘对话流量，签到/刷新/探测照常；冷却中到期自动恢复。健康度：剩余积分占比四态（已知百分比/未探测/无探测/已耗尽）。未探测＝探测失败或渠道未给额度信息（点「探测」可重试），≠已耗尽；无探测＝免费层上游根本没有额度接口。调度器优先选百分比高者，打平时再用账户剩余积分多者。" />} />
                <SortableHead label="额度" columnKey="quota_remaining"
                              active={sort === "quota_remaining"} direction={order}
                              onToggle={toggle} testId="sort-quota_remaining"
                              hint={<ColumnHint text="CodeBuddy 本周期剩余按日期重置；TRAE 账户剩余单调递减。到期额度行＝调度窗口内即将过期、会被优先消耗的额度；主窗口（36h）打平时才比较次窗口（7 天）。CodeArts 的额度是 token（每日 1000 万池、0 点清零），其余渠道是积分。数字后的下箭头展开该凭证的积分记录（两次额度探测之间的净变化）。" />} />
                <SortableHead label="token 剩余" columnKey="token_expires_at"
                              active={sort === "token_expires_at"} direction={order}
                              onToggle={toggle} testId="sort-token_expires_at"
                              hint={<ColumnHint text="access token 距离到期还有多久，取自凭证本身（为 0 表示渠道未给到期信息，显示 —）。预刷新任务每小时检查一次，进入 24 小时窗口即自动续期；「已过期」意味着上游会拒绝该凭证，需重新登录。" />} />
                <SortableHead label="成长中心" columnKey="growth_last_run_at"
                              active={sort === "growth_last_run_at"} direction={order}
                              onToggle={toggle} testId="sort-growth_last_run_at"
                              hint={<ColumnHint text="仅 CodeBuddy：最近一轮成长中心（旅行礼物/任务/连登兑换/盲盒）的领取结果与时间，由定时任务或手动执行写入。汇报较长时单行截断，悬浮看全文。" />} />
                {canWrite && <TableHead className="text-right"><span className="inline-flex items-center gap-1">操作<ColumnHint text="探测/签到：行内常驻按钮。更多操作（⋯）：成长中心、活跃上报（均仅 CodeBuddy）、指定、暂停/恢复、删除。积分记录入口在额度列数字后的下箭头。" /></span></TableHead>}
              </TableRow>
            </TableHeader>
            <TableBody>
              {credentials.map((credential) => (
                <Fragment key={credential.id}>
                  <Row
                    credential={credential}
                    now={now}
                    expiryWindow={expiryWindow}
                    expirySecondaryWindow={expirySecondaryWindow}
                    tokenWarning={tokenWarning}
                    canWrite={canWrite}
                    busy={busy}
                    confirming={confirmDelete === credential.id}
                    creditsOpen={creditEvents?.credentialId === credential.id}
                    onConfirmDelete={setConfirmDelete}
                    actions={actions}
                  />
                  {creditEvents?.credentialId === credential.id && (
                    <TableRow data-testid={`credit-row-${credential.id}`}>
                      <TableCell colSpan={canWrite ? 6 : 5} className="p-0">
                        <CreditDrawer
                          credentialId={credential.id}
                          events={creditEvents.events}
                          sort={creditEvents.sort}
                          order={creditEvents.order}
                          onSort={sortCredits}
                          onClose={() => actions.credits(credential)}
                        />
                      </TableCell>
                    </TableRow>
                  )}
                </Fragment>
              ))}
            </TableBody>
          </Table>
        )}
      </Panel>

      {canWrite && (
        <AddCredentialDialog
          open={addOpen}
          onClose={() => setAddOpen(false)}
          credentialCount={credentials.length}
          hasZen={hasZen}
          hasKilo={hasKilo}
          onImported={(message) => {
            setAddOpen(false);
            showNotice(message);
            void refresh();
          }}
          onNotice={showNotice}
          onError={showError}
        />
      )}
    </div>
  );
}

/** 额度包明细：一个账号常有几十个各自独立到期的积分/权益包，
 * 汇总（剩余/总量）看不出"哪些包、什么时候过期"。表格里只在首行
 * 「剩余 / 总量」右侧留一个「套餐 N 个」，鼠标悬浮才弹出完整明细——
 * 几十行不能挤进单元格。无明细（未探测/上游未提供）时不渲染。
 */
function PackageLadder({ credential }: { credential: Credential }) {
  const packages = credential.quota_packages ?? [];
  if (packages.length === 0) return null;
  // 展示按到期升序：快过期的排最前（无到期信息的排最后）
  const ordered = [...packages].sort(
    (left, right) => (left.end ?? Infinity) - (right.end ?? Infinity),
  );
  return (
    <LongTextTip
      // 明细行（包名 + 剩余/总量 + 已用 + 到期）比默认 max-w-sm 宽得多，
      // 用窄弹层会每行折成两三行；这里放宽并禁止折行，超出视口时内层横向滚动。
      className="max-w-[90vw] whitespace-nowrap"
      content={
        <div
          data-testid={`packages-${credential.id}`}
          className="max-h-[70vh] max-w-full overflow-x-auto overflow-y-auto pr-1"
        >
          <div className="mb-1 font-medium">
            额度包 {packages.length} 个（按到期先后）
          </div>
          <ul className="space-y-0.5">
            {ordered.map((item, index) => (
              <li key={`${item.name}-${item.end}-${index}`} data-testid={`package-${credential.id}`}>
                {item.name ? `${item.name}：` : ""}
                剩 {formatNumber(item.total - item.used)} / {formatNumber(item.total)}
                {item.used > 0 && `（已用 ${formatNumber(item.used)}）`}
                · {packageExpiry(item.end)} 到期
              </li>
            ))}
          </ul>
        </div>
      }
    >
      <Button
        type="button"
        variant="link"
        className="h-auto cursor-help px-0 text-left text-xs decoration-dotted underline-offset-2"
        data-testid={`packages-toggle-${credential.id}`}
      >
        套餐 {packages.length} 个
      </Button>
    </LongTextTip>
  );
}
/** 生效中的模型级冷却（6004 模型限流 / 11102 该账号无此模型）。
 *
 * 与账号级「冷却中」状态是两回事：整条凭证仍可用，只是这些模型被避开。
 * 表格里只留一个警告图标，具体是哪些模型、还剩多久、连续几次都在 hover 里——
 * 逐条铺开会把额度列撑成好几行（限流时往往同时避让多个模型）。
 */
function ModelCooldownList({ credential, now }: { credential: Credential; now: number }) {
  const items = activeModelCooldowns(credential, now);
  if (items.length === 0) return null;
  return (
    <LongTextTip
      content={
        <div className="space-y-0.5" data-testid={`model-cooldowns-${credential.id}`}>
          {items.map((item) => (
            <div key={item.model}>
              模型 {item.model}：{modelCooldownLabel(item)}，
              {formatDuration(Math.round(item.cooling_until - now))}后重试
              {item.hits > 1 && `（连续 ${item.hits} 次）`}
            </div>
          ))}
        </div>
      }
    >
      <span
        className="inline-flex cursor-help items-center text-warn"
        data-testid={`model-cooldown-icon-${credential.id}`}
        aria-label={`模型避让 ${items.length} 个`}
      >
        <TriangleAlert className="size-3.5" />
      </span>
    </LongTextTip>
  );
}

/**
 * access token 到期展示（B3.3）。
 *
 * 只说一件事：还剩多久。进度条与「最后续期」都不给：
 * - 进度条的量纲是 token 自己的寿命（`exp - iat`），两个渠道分别是 55 天和
 *   14 天，同一根条在不同渠道间没有可比性；而「还剩几天」本身已经回答了
 *   调度关心的唯一问题。
 * - 「最后续期」需要读者自己拿它与剩余天数做二次推理（刚续期 vs 没人管），
 *   属于解释性信息，不该占表格里的一行。
 *
 * 到期时间未知（后端 0）时显示「—」（与「成长中心」列一致）：渠道没给到期
 * 信息不等于马上过期，但也确实无可展示。
 */
function TokenExpiry({
  credential,
  view,
}: {
  credential: Credential;
  view: TokenExpiryView;
}) {
  if (view.remaining === null) return <span className="text-muted-foreground">—</span>;
  return (
    <div
      className={view.expiring ? "text-destructive" : "text-muted-foreground"}
      data-testid={`token-expiry-${credential.id}`}
    >
      {view.remaining <= 0 ? "已过期" : view.label}
      {view.expiring && view.remaining > 0 && "，即将到期"}
    </div>
  );
}

function Row({
  credential,
  now,
  expiryWindow,
  expirySecondaryWindow,
  tokenWarning,
  canWrite,
  busy,
  confirming,
  creditsOpen,
  onConfirmDelete,
  actions,
}: {
  credential: Credential;
  now: number;
  expiryWindow: number;
  expirySecondaryWindow: number;
  tokenWarning: number;
  canWrite: boolean;
  busy: boolean;
  confirming: boolean;
  creditsOpen: boolean;
  onConfirmDelete: (id: string | null) => void;
  actions: Actions;
}) {
  const expiring = expiringQuotaLabel(
    credential.quota_expiring_credits, expiryWindow, "primary", QUOTA_UNIT);
  // 次窗口只在主窗口没有可展示内容时才渲染：7 天窗口是 36h 的超集，
  // 主窗口已有数字时再列一行只会重复。它的作用是解释「36h 内没有
  // 到期积分、但一周内会过期」的账号为何仍被优先选中。
  const expiringSecondary = expiring
    ? null
    : expiringQuotaLabel(
        credential.quota_expiring_credits_secondary,
        expirySecondaryWindow,
        "secondary",
        QUOTA_UNIT,
      );
  const health = healthView(credential.health, credential.provider);
  const state = credentialState(credential, now);
  const cooldown = cooldownRemaining(credential.cooling_until, now);
  const tokenExpiry = tokenExpiryView(
    credential.token_expires_at, tokenWarning, now, credential.token_issued_at);

  // 抽屉由调用方渲染成**独立的下一行**（见 TableBody）：塞进本行的最后一个单元格
  // 只会挤在「操作」列里——同行内的 colSpan 不生效，整行高度会被拉坏。
  return (
    <TableRow data-testid={`row-${credential.id}`}>
      <TableCell>
        {/* 昵称 + 渠道合并成一列：渠道名做小字副行，省一列横向空间 */}
        <div className="flex items-center gap-2">
          <ProviderIcon provider={credential.provider} size={14} />
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span
                className="truncate font-medium"
                title={credential.nickname || credential.id.slice(0, 12)}
              >
                {credential.nickname || credential.id.slice(0, 12)}
              </span>
              {credential.pinned === 1 && <Badge tone="accent">已指定</Badge>}
            </div>
            <div className="text-xs text-muted-foreground">
              {PROVIDER_LABEL[credential.provider]}
            </div>
          </div>
        </div>
      </TableCell>
      <TableCell>
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
      <TableCell className="text-xs">
        <div className="flex flex-wrap items-baseline gap-x-2">
          {/* 数字与积分记录入口同处一个 inline-flex：按钮跟数字做行内居中对齐，
              而不是与整行 baseline 对齐（图标按钮没有文字基线，会明显偏低）。 */}
          <span className="inline-flex items-center gap-1">
            <span>
              {formatNumber(credential.quota_remaining)} / {formatNumber(credential.quota_total)}
            </span>
            {/* 积分记录入口紧贴它要解释的数字：曾经在「更多操作」菜单里，
                打开前看不到任何余额上下文。下箭头是行内展开语义（非弹层），
                展开后箭头翻转。仅 admin/operator 可用——接口本身就是该门槛。
                zen / kilo 免费层没有额度接口、也没有额度探测，积分记录恒为空，
                入口只会给出「无记录」的空抽屉，直接不渲染。 */}
            {canWrite && hasQuotaProbe(credential.provider) && (
              <Button
                // 项目封装的 Button：variant="default" 即 shadcn 的 outline
                variant="default"
                size="icon"
                aria-label="积分记录"
                aria-expanded={creditsOpen}
                title="积分记录：两次额度探测之间的净变化"
                data-testid={`credits-${credential.id}`}
                onClick={() => actions.credits(credential)}
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
          <PackageLadder credential={credential} />
        </div>
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
        <div className="text-muted-foreground">{quotaSemantics(credential)}</div>
      </TableCell>
      <TableCell className="text-xs">
        <TokenExpiry credential={credential} view={tokenExpiry} />
      </TableCell>
      <TableCell className="text-xs text-muted-foreground">
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
      {canWrite && (
        <TableCell>
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
                    签到现全渠道支持（CodeArts 自 Q72 起接入）。 */}
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
                    {credential.provider === "codebuddy" && (
                      <DropdownMenuItem
                        onSelect={() => actions.growth(credential)}
                        title="手动跑一轮成长中心领取（与定时任务同一条路径）"
                      >
                        <Sparkles className="size-4" /> 成长中心
                      </DropdownMenuItem>
                    )}
                    {credential.provider === "codebuddy" && (
                      <DropdownMenuItem onSelect={() => actions.activity(credential)}>
                        <Radio className="size-4" /> 活跃上报
                      </DropdownMenuItem>
                    )}
                    <DropdownMenuSeparator />
                    {credential.disabled === 1 && (
                      <DropdownMenuItem onSelect={() => actions.revive(credential)}>
                        <HeartPulse className="size-4" /> 恢复
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

/**
 * 积分记录抽屉（B3.4）：同一行内展开，不弹层。
 *
 * 语义纪律：这里展示的是**两次额度探测之间的净变化**，不是动作归因。上游
 * 签到/成长接口不打日志，diff 看不到分数是谁加的，所以文案一律说「净变化」，
 * 只有归因已知度（observed/sync）。把净变化说成「签到获得」就是拿猜测当事实。
 */
function CreditDrawer({
  credentialId,
  events,
  sort,
  order,
  onSort,
  onClose,
}: {
  credentialId: string;
  events: CreditEvent[];
  sort: string;
  order: string;
  onSort: (columnKey: string) => void;
  onClose: () => void;
}) {
  return (
    <Card
      className="mt-4 gap-0 bg-muted/30 p-3 ring-border/60"
      data-testid={`credit-drawer-${credentialId}`}
    >
      <div className="mb-2 flex items-center justify-between">
        <div className="text-sm font-medium">
          积分记录
          <span className="ml-2 text-xs text-muted-foreground">
            两次额度探测之间的净变化（非动作归因）
          </span>
        </div>
        <Button size="sm" variant="ghost" onClick={onClose} data-testid="credit-drawer-close">
          关闭
        </Button>
      </div>
      {events.length === 0 ? (
        <Empty data-testid="credit-drawer-empty">
          还没有记录。余额变化会在下一轮额度探测时写入（首次探测只建立基线）。
        </Empty>
      ) : (
        <Table data-testid="credit-drawer-table">
          <TableHeader>
            <TableRow>
              <SortableHead label="观测时间" columnKey="ts" active={sort === "ts"}
                            direction={order as "asc" | "desc"} onToggle={onSort}
                            testId="sort-credit-ts" />
              <SortableHead label="区间起点" columnKey="window_start"
                            active={sort === "window_start"} direction={order as "asc" | "desc"}
                            onToggle={onSort} testId="sort-credit-window_start" />
              <SortableHead label="变化" columnKey="delta" active={sort === "delta"}
                            direction={order as "asc" | "desc"} onToggle={onSort}
                            testId="sort-credit-delta" />
              <SortableHead label="说明" columnKey="source" active={sort === "source"}
                            direction={order as "asc" | "desc"} onToggle={onSort}
                            testId="sort-credit-source" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {events.map((event) => (
              <TableRow key={event.id} data-testid={`credit-event-${event.id}`}>
                <TableCell className="text-xs">{formatTime(event.ts)}</TableCell>
                <TableCell className="text-xs text-muted-foreground">
                  {event.window_start ? formatTime(event.window_start) : "—"}
                </TableCell>
                <TableCell
                  className={`text-xs ${
                    event.delta === null
                      ? "text-muted-foreground"
                      : event.delta > 0
                        ? "text-ok"
                        : event.delta < 0
                          ? "text-warn"
                          : "text-muted-foreground"
                  }`}
                >
                  {creditEventLabel(event)}
                </TableCell>
                <TableCell className="text-xs text-muted-foreground">
                  {CREDIT_SOURCE_LABEL[event.source] ?? event.source}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </Card>
  );
}
