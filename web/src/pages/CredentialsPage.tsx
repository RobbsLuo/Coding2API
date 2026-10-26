import { Fragment, useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  Activity,
  ArrowDownIcon,
  Ban,
  CalendarCheck,
  CalendarClock,
  ChevronRight,
  CircleCheck,
  Coins,
  Database,
  Flame,
  Gauge,
  HeartPulse,
  Hourglass,
  MoreHorizontal,
  Pin,
  Plus,
  Power,
  Radio,
  RefreshCw,
  Search,
  Sparkles,
  Tag,
  Timer,
  ToggleLeft,
  Trash2,
  TriangleAlert,
} from "lucide-react";
import { cn } from "@/lib/utils";
import {
  ANY,
  DEFAULT_FILTERS,
  DEFAULT_GROUP_SORT,
  groupByProvider,
  matchesFilters,
  providerOptions,
  sortGroups,
  STATE_ORDER,
  stateOptions,
  tagOptions,
} from "../lib/credentialList";
import type { GroupSort, Ledger } from "../lib/credentialList";
import { api } from "../api/client";
import { useSessionContext } from "../Layout";
import { useCredentials, usePlaygroundModels, useQueryClient } from "../api/hooks";
import { PoolOverview } from "../components/PoolOverview";
import { AddCredentialDialog } from "../components/AddCredentialDialog";
import { CredentialLimitsDialog } from "../components/CredentialLimitsDialog";
import { CredentialTagsDialog } from "../components/CredentialTagsDialog";
import { groupModelsByProvider, rateLabel, rateSummary } from "../components/ModelPicker";
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
import type { CredentialState, TokenExpiryView } from "../api/display";
import type { BatchAction, BatchResult, Credential, CreditEvent, GrowthRunResult } from "../api/types";
import {
  Badge,
  Button,
  Card,
  Checkbox,
  Empty,
  EmptyState,
  Input,
  Notice,
  Panel,
  Select,
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
 * CodeArts 已接入 `/v1/ops/delivery`（此前「上游没有签到接口」的结论
 * 已推翻）。CodeBuddy 国际站（`codebuddy_intl`）**不支持**：上游
 * `/billing/meter/checkin-activity-status` 回 `active: false` 且活动字段全空
 * （渠道侧无此活动，非账号级未开启），后端相应把该渠道的 `checkin*` 置 None，
 * 这里同步隐藏按钮，避免给一个必然失败的操作出口。
 */
function supportsCheckin(provider: Credential["provider"]): boolean {
  return provider !== "codebuddy_intl";
}

/**
 * 渠道是否支持活跃上报（手动补发一条对话事件）。
 *
 * 后端按能力探测（`getattr(provider, "activity", None)`），国际站已启用——
 * 活跃上报打的 `/v2/report` 与成长中心的 `/v2/activity/*` 不是同一路径，
 * 实测国际站回 `200 {"code":0}`，故 `intl.py` 不再把 `activity` 置 None。
 * 前端据此不能用 `provider === "codebuddy"` 写死：那会让国际版菜单项消失，
 * 而后端其实可以跑（用户报的正是这个错配）。
 */
function supportsActivity(provider: Credential["provider"]): boolean {
  return provider === "codebuddy" || provider === "codebuddy_intl";
}

/**
 * 渠道是否有「成长中心」这一列。
 *
 * 后端按能力探测（`getattr(provider, "growth", None)`），目前只有 CodeBuddy 有
 * （国际站 `intl.py` 显式 `growth = None`）。与行内菜单项的判定同源，避免
 * 「菜单里没有成长中心、列却常驻一列 —」的错配。
 */
function supportsGrowth(provider: string): boolean {
  return provider === "codebuddy";
}

/** 套餐到期日：只要日期，时分秒对"哪个包先过期"没用。 */
function packageExpiry(epoch: number | null): string {
  return epoch === null ? "—" : new Date(epoch * 1000).toLocaleDateString("zh-CN");
}

/**
 * 批量条按钮：后端动作 + 按钮自身文案。
 *
 * 按钮与后端动作**不是一一对应**：`toggle` 要拆成「暂停」（enabled=false）与
 * 「取消暂停」（enabled=true）两个按钮——只给一个按钮的话，操作员把号批量
 * 暂停后就再也没有入口批量放回来（`revive` 清的是 `disabled` 硬禁用，不碰
 * `enabled`，救不了暂停态）。因此按钮有独立 `key` 用于 testid 与文案查表，
 * `action` 才是发给后端的动作名。
 *
 * `verb` 用于按钮；`done` 用于结果句。放一张表而不是散在各处的 if/else：
 * 文案要在按钮与结果汇总两处保持一致，写歪一处就会出现「批量探测」按钮却
 * 弹出「批量签到完成」。
 */
type BatchButton = {
  key: string;
  action: BatchAction;
  /** 仅 toggle 需要：显式目标状态，不做「各自取反」。 */
  enabled?: boolean;
  verb: string;
  done: string;
};

const BATCH_BUTTONS: readonly BatchButton[] = [
  { key: "probe", action: "probe", verb: "探测额度", done: "探测完成" },
  { key: "checkin", action: "checkin", verb: "每日签到", done: "签到完成" },
  { key: "pause", action: "toggle", enabled: false, verb: "暂停", done: "已暂停" },
  { key: "resume", action: "toggle", enabled: true, verb: "取消暂停", done: "已取消暂停" },
  // 「恢复」= revive（解除 session 死亡硬禁用），与「取消暂停」是两件事；
  // 单条菜单里已按同样口径区分，批量条保持一致。
  { key: "revive", action: "revive", verb: "恢复", done: "已恢复" },
];

/**
 * 批量结果浮层：成功数 + 失败明细。
 *
 * 失败原因**逐条列出**而不是只报「失败 3 条」：批量失败几乎总是同一类原因
 * （凭证失效 / 渠道不支持），但混批时可能几种并存，只给计数等于让用户自己
 * 去翻是哪几条。列表上限 5 条，更多的折成「另有 N 条」。
 */
function BatchToastContent({
  result,
  names,
  done,
}: {
  result: BatchResult;
  names: Map<string, string>;
  /**
   * 结果句的措辞，由**触发按钮**给出而非 `result.action` 反查：
   * 「暂停」与「取消暂停」共用 action=toggle，按 action 查表会把取消暂停的
   * 成功提示写成「已暂停」。
   */
  done: string;
}) {
  const failures = result.results.filter((row) => !row.ok);
  return (
    <div className="space-y-1">
      <div className="font-medium">
        {done}：成功 {result.succeeded} / {result.total}
        {result.failed > 0 && `，失败 ${result.failed}`}
      </div>
      {failures.length > 0 && (
        <ul className="space-y-0.5 text-xs text-muted-foreground">
          {failures.slice(0, 5).map((row) => (
            <li key={row.id} data-testid="batch-failure">
              {names.get(row.id) ?? row.id.slice(0, 12)}：
              {batchRowReason(result.action, row)}
            </li>
          ))}
          {failures.length > 5 && <li>另有 {failures.length - 5} 条失败</li>}
        </ul>
      )}
    </div>
  );
}

/**
 * 一条批量失败的中文原因。
 *
 * 三种来源的优先级：`error`（没执行到上游：凭证不存在 / 渠道不支持 / 意外异常）
 * → probe 的 `reason`（执行了但上游拒绝，是正常结果）→ checkin 的 `message`。
 * 顺序不能颠倒：probe 失败时 detail.reason 与 error 不会同时存在，但若未来
 * 两者都在，`error` 更接近「这条根本没跑」的事实。
 */
function batchRowReason(action: BatchAction, row: BatchResult["results"][number]): string {
  if (row.error) {
    if (row.error === "credential not found") return "凭证不存在（可能已删除）";
    if (row.error === "action_failed") return "执行失败，详见服务端日志";
    if (row.error.startsWith("credential does not support")) return "该渠道不支持此操作";
    return row.error;
  }
  if (action === "probe" && row.detail && "reason" in row.detail) {
    return probeFailureLabel(row.detail.reason);
  }
  if (action === "checkin" && row.detail && "message" in row.detail && row.detail.message) {
    return row.detail.message;
  }
  return "失败";
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

/** 一句话汇报：登录失效要单独提示「重新登录」，不能与普通失败混同。 */
function growthNotice(result: GrowthRunResult): string {
  if (result.session_dead) return "登录态已失效，需重新登录该渠道";
  return result.report || (result.ok ? "成长中心执行完成" : "成长中心未完全成功");
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
        <div className="text-xs">登录态已失效，需重新登录该渠道后才能领取。</div>
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
  credits: (credential: Credential, ledger: Ledger) => void;
  limits: (credential: Credential) => void;
  tags: (credential: Credential) => void;
}

export function CredentialsPage() {
  const session = useSessionContext();
  // 默认 created_at 升序（与后端一致）；额度 / 健康度等数值列首点降序
  const { sort, order, toggle } = useSort("created_at", "asc",
    { health: "desc", quota_remaining: "desc",
      token_expires_at: "asc", growth_last_run_at: "desc" });
  // 30s 轮询：冷却倒计时 / token 剩余都是「随时间变化」的观测量，页面停留
  // 时应自动刷新（与 useTasks 同口径）。
  const { data, isLoading, isError } = useCredentials(session.username, 30_000, sort, order);
  // 组头「模型 N 个」：与 Playground / `/v1/models` 同源（只含有可用凭证且过
  // 当前黑名单的渠道），因此组里「有凭证」与「有模型」是同一口径，不必另算一套。
  const modelsQuery = usePlaygroundModels();
  const client = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  // 添加凭证对话框（登录渠道 / JSON 导入 / 免费层补回都收在里面）
  const [addOpen, setAddOpen] = useState(false);
  // 限额护栏对话框：一次只编辑一条（存凭证 id，轮询替换列表后仍能取到最新值）
  const [limitsFor, setLimitsFor] = useState<string | null>(null);
  // 标签编辑对话框：同样存 id，取最新快照
  const [tagsFor, setTagsFor] = useState<string | null>(null);
  // 列表筛选：池子一大就没有「找到某个号」的路径，这里收搜索 + 渠道 + 状态 + 标签四件套。
  // 筛选是纯展示层收敛，不改后端排序（后端仍按 sort/order 给全量）。
  const [filters, setFilters] = useState(DEFAULT_FILTERS);
  // 组排序：默认全站渠道顺序（固定，不随轮询跳动）；可切到「额度从高到低」，
  // 先看还能打最多的渠道。仅重排组，组内行序仍由后端 sort/order 决定。
  const [groupSort, setGroupSort] = useState<GroupSort>(DEFAULT_GROUP_SORT);
  // 展开的渠道分组（值为渠道 id）。默认全折叠：组头已经给出「几个号、状态分布、
  // 多少额度、多少即将过期」，先看渠道级结构，需要看具体账号时再展开——池子一大，
  // 首屏铺开全部行反而看不出结构。筛选生效时自动展开命中的渠道（见 applyFilters），
  // 否则「搜完却看不到结果」。
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  // 批量运维的多选集合。只存 id：凭证列表每 30s 轮询替换，存对象会拿到过期快照。
  // 删除/筛选变化后残留的 id 在渲染时与当前列表求交（见 selectedIds），
  // 不主动清理——清理需要 effect，而求交是纯计算，没有「状态不同步」的窗口。
  const [selected, setSelected] = useState<Set<string>>(new Set());
  // 操作结果统一走顶部居中浮层（toast），不再以插入式横幅撑开页面：
  // error 与 notice 各占一个槽位，同类提示只保留最新一条。
  const { toasts, push, dismiss } = useToasts();
  const showError = (content: ReactNode) => push({ tone: "danger", testId: "credentials-error", content });
  const showNotice = (content: ReactNode) => push({ tone: "ok", testId: "credentials-notice", content });
  // 变动流水抽屉：一次只开一个（同一行内展开，不弹层——
  // 表格行本身就是最好的上下文，弹层会遮掉额度列）。`ledger` 记下打开时
  // 读的是哪本账（CodeArts 读积分账 credits，其余渠道读 pool，
  // 见 `providerLedger`），换排序重新取数时必须带同一个账本，否则会串到
  // 另一本账的流水。
  const [creditEvents, setCreditEvents] = useState<{
    credentialId: string;
    ledger: Ledger;
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

  // 先筛后分组：分组只是把筛选结果按渠道归类，不改变「哪些该出现」。
  const filtered = useMemo(
    () => credentials.filter((credential) => matchesFilters(credential, filters, now)),
    // now 每 30s 轮询才变，纳入依赖是为了让「冷却中」筛选在到点后自动收放
    [credentials, filters, now],
  );
  const groups = useMemo(
    () => sortGroups(groupByProvider(filtered, now), groupSort),
    [filtered, now, groupSort],
  );
  const providerCounts = useMemo(() => providerOptions(credentials), [credentials]);
  const stateCounts = useMemo(
    () => stateOptions(credentials, now, filters.state),
    [credentials, now, filters.state],
  );
  const tagCounts = useMemo(() => tagOptions(credentials), [credentials]);
  // 渠道 → 模型清单（组头展示用）。模型列表与凭证列表各自轮询 / 缓存，取不到时
  // 为空 Map：组头那块直接不渲染，而不是显示「模型 0 个」（0 会被读成「这渠道没模型」）。
  const modelsByProvider = useMemo(
    () => groupModelsByProvider(modelsQuery.data?.data ?? []),
    [modelsQuery.data],
  );
  // 选中集合与当前列表求交：凭证被删除 / 轮询后消失时，残留 id 自动失效，
  // 不会出现「批量操作 3 条，其中 1 条已不存在」。用 filtered 而不是 credentials：
  // 筛选后不可见的行不应被批量动作带上（用户看不见就不会预期它被执行）。
  const selectedIds = useMemo(
    () => filtered.filter((credential) => selected.has(credential.id)),
    [filtered, selected],
  );
  // 批量结果里要把 id 换成人能认出的昵称
  const credentialNames = useMemo(
    () => new Map(credentials.map((item) => [item.id, item.nickname || item.id.slice(0, 12)])),
    [credentials],
  );
  // 限额对话框当前编辑的凭证：按 id 从最新列表取，轮询替换后仍是最新快照
  const limitsCredential = useMemo(
    () => credentials.find((item) => item.id === limitsFor) ?? null,
    [credentials, limitsFor],
  );
  // 标签对话框当前编辑的凭证：同上，按 id 取最新快照
  const tagsCredential = useMemo(
    () => credentials.find((item) => item.id === tagsFor) ?? null,
    [credentials, tagsFor],
  );
  // 是否有任何筛选生效：决定「N / 总数」计数与「清空」入口是否出现
  const filtering =
    filters.query.trim() !== "" || filters.provider !== ANY ||
    filters.state !== ANY || filters.tag !== ANY;

  /**
   * 更新筛选并同步展开态。
   *
   * 两个方向都要照顾：
   * - 筛选**生效**时展开命中的渠道——否则「搜到了但看不到行」；
   * - 筛选**清空**时收回全折叠——回到默认的渠道级总览。
   * 组头始终可见，所以「先折叠、要看时点开」在无筛选时仍然成立。
   */
  const applyFilters = (next: typeof filters) => {
    setFilters(next);
    if (next.query.trim() !== "" || next.provider !== ANY ||
        next.state !== ANY || next.tag !== ANY) {
      setExpanded(new Set(groupByProvider(
        credentials.filter((item) => matchesFilters(item, next, now)), now,
      ).map((group) => group.key)));
    } else {
      setExpanded(new Set());
    }
  };

  const toggleGroup = (key: string) => {
    setExpanded((previous) => {
      const next = new Set(previous);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  // 凭证增删 / 暂停会改变「哪些渠道还有可用凭证」，组头的模型清单（与 Playground
  // 共用的 `playground-models` 缓存）必须一起失效，否则凭证列表刷新了、模型块还
  // 停在旧数据上（新接入的渠道组头一直空着）。
  const refresh = () => Promise.all([
    client.invalidateQueries({ queryKey: ["admin"] }),
    client.invalidateQueries({ queryKey: ["playground-models"] }),
  ]);

  // 流水抽屉内排序：改排序需重新取数（后端排），并保持方向直觉
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
      const result = await api.creditEvents(
        creditEvents.credentialId, creditEvents.ledger, columnKey, order);
      setCreditEvents({ ...creditEvents, events: result.events, sort: columnKey, order });
    } catch (caught) {
      showError(caught instanceof Error ? caught.message : "流水读取失败");
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

  /**
   * 批量动作：一次提交所有选中项，结果**逐条**汇报。
   *
   * 与逐行循环调单条端点的区别在于错误处理：这里一次请求拿到「成功 N / 失败 M」
   * 与失败明细，而不是在第 3 条报错时丢掉前两条的结果。选中集合在成功后清空——
   * 同一批号再点一次「签到」没有意义；失败项也一并清空，避免用户以为要重试
   * 却把成功项又跑一遍（失败原因通常需要先修凭证）。
   */
  const runBatch = async (button: BatchButton) => {
    const ids = selectedIds.map((credential) => credential.id);
    if (ids.length === 0) return;
    setBusy(true);
    try {
      const result = await api.batchCredentials(button.action, ids, button.enabled);
      const tone = result.failed > 0 ? "warn" : "ok";
      push({
        tone,
        testId: "credentials-batch-result",
        content: <BatchToastContent result={result} names={credentialNames} done={button.done} />,
      });
      setSelected(new Set());
      await refresh();
    } catch (caught) {
      showError(caught instanceof Error ? caught.message : "批量操作失败");
    } finally {
      setBusy(false);
    }
  };

  /** 勾选 / 取消勾选一条；用于行内 checkbox 与组头「全选本渠道」。 */
  const toggleSelected = (credentialId: string, checked: boolean) => {
    setSelected((previous) => {
      const next = new Set(previous);
      if (checked) next.add(credentialId);
      else next.delete(credentialId);
      return next;
    });
  };

  /** 组头「全选本渠道」：把该组所有行一次性纳入 / 移出选中集合。 */
  const toggleGroupSelected = (ids: string[], checked: boolean) => {
    setSelected((previous) => {
      const next = new Set(previous);
      for (const id of ids) {
        if (checked) next.add(id);
        else next.delete(id);
      }
      return next;
    });
  };

  const actions: Actions = {
    // 会话失效被硬禁用的凭证：重新登录后用「恢复」放回池子，
    // 不必删除重建（以前只能删除）。
    revive: (credential) =>
      void run(() => api.reviveCredential(credential.id), "已恢复该凭证，可参与调度。"),
    toggle: (credential) =>
      void run(
        () => api.toggleCredential(credential.id, credential.enabled !== 1),
        // 文案与「已暂停」状态一致：只摘对话流量，后台任务不受影响
        credential.enabled === 1
          ? "已暂停该凭证的对话流量；签到 / 刷新 / 探测照常。"
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
              `探测失败：${probeFailureLabel(result.reason)}（健康度保持为「未探测」）`,
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
    // 只读：开关抽屉不写任何状态，因此不复用 run()（那个会刷新列表）。
    // `ledger` 由调用行给出（CodeArts＝积分账，其余＝pool）：两本账必须分开读。
    credits: (credential, ledger) =>
      void (async () => {
        if (creditEvents?.credentialId === credential.id
            && creditEvents.ledger === ledger) {
          setCreditEvents(null);
          return;
        }
        try {
          const result = await api.creditEvents(credential.id, ledger);
          setCreditEvents({
            credentialId: credential.id, ledger, events: result.events,
            sort: "ts", order: "desc",
          });
        } catch (caught) {
          showError(caught instanceof Error ? caught.message : "流水读取失败");
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
    // 只开对话框；保存与刷新由对话框自己回调（见 CredentialLimitsDialog）
    limits: (credential) => setLimitsFor(credential.id),
    tags: (credential) => setTagsFor(credential.id),
  };

  if (isLoading) return <PageSkeleton variant="cards" />;

  return (
    <div className="space-y-6" data-testid="credentials-page">
      <PageHeader
        eyebrow="控制台"
        title="凭证管理"
        description="凭证＝调度池里可被选中的渠道账号。登录授权或粘贴 JSON 导入后可探测额度、签到、启停；顶部池概况按健康度四态说明还剩多少可用。"
        icon={<Database className="size-5" />}
      />
      {/* 池概览（原「池仪表盘」页内容合并于此）：统计卡 + 健康度四态分布 */}
      <PoolOverview credentials={credentials} now={now} />
      {!canWrite && (
        <div data-testid="readonly-banner">
          <Notice tone="muted">只读模式：仅管理员与操作员可以导入、启停或删除凭证。</Notice>
        </div>
      )}
      {isError && (
        <div data-testid="credentials-load-error">
          <Notice tone="danger">
            凭证列表加载失败：服务可能暂时不可用或会话已过期。下面显示「没有凭证」
            是取不到数据，不是真的空池——别据此以为凭证丢了。
          </Notice>
        </div>
      )}
      {!isError && credentials.length === 0 && canWrite && (
        <div data-testid="first-run-hint">
          <Notice tone="muted">
            还没有凭证。点凭证池右上角「添加凭证」登录渠道账号（CodeBuddy / TRAE /
            Qoder / CodeArts），或直接粘贴凭证 JSON 导入；OpenCode Zen / Kilo
            Gateway 免费层在对话框里一键添加。
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
        {isError ? (
          // 加载失败不是「空池」：给出错误态，不显示「还没有凭证」
          <EmptyState
            icon={<Database className="size-5" />}
            title="凭证列表加载失败"
            description="服务暂时不可用或会话已过期，请稍后重试。"
            data-testid="credentials-error-state"
          />
        ) : credentials.length === 0 ? (
          <EmptyState
            icon={<Database className="size-5" />}
            title="还没有凭证"
            description="用右上角「添加凭证」登录渠道账号或导入凭证 JSON 即可开始调度。"
            data-testid="no-credentials"
          />
        ) : (
          <>
            {/* 筛选工具条：池子一大，「找到某个号」就没有路径了。搜索 + 渠道 +
                状态 + 标签四件套都是纯展示层收敛，不改后端排序（后端仍按
                sort/order 给全量）。「筛选后 N / 总数」只在真的筛了才出现，
                不筛时不制造噪声。 */}
            <div className="flex flex-wrap items-center gap-2 pb-3" data-testid="credentials-toolbar">
              <div className="relative">
                <Search className="pointer-events-none absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-muted-foreground" />
                <Input
                  value={filters.query}
                  data-testid="credential-search"
                  aria-label="搜索凭证"
                  placeholder="搜索昵称 / 渠道 / id"
                  className="h-8 w-full pl-7 text-xs sm:w-52"
                  onChange={(event) => applyFilters({ ...filters, query: event.target.value })}
                />
              </div>
              <Select
                value={filters.provider}
                data-testid="credential-provider-filter"
                aria-label="按渠道筛选"
                className="h-8 w-40 text-xs"
                onChange={(event) => applyFilters({ ...filters, provider: event.target.value })}
              >
                <option value={ANY}>全部渠道（{credentials.length}）</option>
                {providerCounts.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}（{option.count}）
                  </option>
                ))}
              </Select>
              <Select
                value={filters.state}
                data-testid="credential-state-filter"
                aria-label="按状态筛选"
                className="h-8 w-36 text-xs"
                onChange={(event) => applyFilters({ ...filters, state: event.target.value })}
              >
                <option value={ANY}>全部状态（{credentials.length}）</option>
                {stateCounts.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}（{option.count}）
                  </option>
                ))}
              </Select>
              {/* 标签筛选：只在池里真的出现过标签时才渲染——没有任何标签时
                  多一个恒为「全部标签」的下拉只是噪声。 */}
              {tagCounts.length > 0 && (
                <Select
                  value={filters.tag}
                  data-testid="credential-tag-filter"
                  aria-label="按标签筛选"
                  className="h-8 w-40 text-xs"
                  onChange={(event) => applyFilters({ ...filters, tag: event.target.value })}
                >
                  <option value={ANY}>全部标签（{credentials.length}）</option>
                  {tagCounts.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}（{option.count}）
                    </option>
                  ))}
                </Select>
              )}
              {/* 组排序：只重排「渠道组」，不动组内行序（行序仍由列头 / 后端 sort 决定）。
                  默认「额度从高到低」——先看还能打最多的渠道；额度排序时无额度接口的
                  免费层垫底（它们没有「剩余额度」可比）。 */}
              <Select
                value={groupSort}
                data-testid="credential-group-sort"
                aria-label="渠道分组排序"
                className="h-8 w-40 text-xs"
                onChange={(event) => setGroupSort(event.target.value as GroupSort)}
              >
                <option value="quota">额度从高到低</option>
                <option value="provider">渠道顺序</option>
              </Select>
              {filtering && (
                <>
                  <span
                    className="text-xs text-muted-foreground"
                    data-testid="credential-filter-count"
                  >
                    筛选后 <strong className="tabular-nums text-foreground">{filtered.length}</strong>
                    {" / "}
                    {credentials.length}
                  </span>
                  <Button
                    size="sm"
                    variant="ghost"
                    data-testid="credential-clear-filters"
                    onClick={() => applyFilters(DEFAULT_FILTERS)}
                  >
                    清空
                  </Button>
                </>
              )}
            </div>

            {/* 批量操作条：只在有选中项时出现，不占常驻高度。选中集合与当前
                筛选结果求交（selectedIds），因此「筛选后勾选、再改筛选」不会
                把已隐藏的行带进批量动作。 */}
            {canWrite && selectedIds.length > 0 && (
              <div
                data-testid="credentials-batch-bar"
                className="mb-3 flex flex-wrap items-center gap-2 rounded-md bg-muted/50 px-3 py-2"
              >
                <span className="text-xs text-muted-foreground" data-testid="batch-selected-count">
                  已选 <strong className="tabular-nums text-foreground">{selectedIds.length}</strong> 个
                </span>
                {BATCH_BUTTONS.map((button) => (
                  <Button
                    key={button.key}
                    size="sm"
                    variant="default"
                    disabled={busy}
                    data-testid={`batch-${button.key}`}
                    onClick={() => void runBatch(button)}
                  >
                    {button.verb}
                  </Button>
                ))}
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={busy}
                  data-testid="batch-clear"
                  onClick={() => setSelected(new Set())}
                >
                  取消选择
                </Button>
              </div>
            )}

            {filtered.length === 0 ? (
              <EmptyState
                icon={<Search className="size-5" />}
                title="没有匹配的凭证"
                description="换个关键词或清空筛选查看全部。"
                data-testid="no-credential-match"
              />
            ) : (
              // 长列表限高 + 吸顶列头：列头随每个展开的渠道组渲染（见 ColumnHeaderRow），
              // 滚到第 20 行时列名与操作按钮还在，不必回顶确认「这一列到底是什么」。
              <Table
                data-testid="credentials-table"
                cards
                containerClassName="max-h-[70vh] overflow-auto"
              >
                <TableBody>
                  {groups.map((group) => {
                    const isCollapsed = !expanded.has(group.key);
                    // 到期积分：以「窗口 + 数字」两个图块呈现，比一行长句更像仪表盘。
                    // 主/次窗口措辞区分开（主窗口 36h 是次窗口 7 天的子集）。
                    const primaryWindowLabel = `${formatDuration(expiryWindow)}内过期`;
                    const secondaryWindowLabel = `${formatDuration(expirySecondaryWindow)}内过期`;
                    // 到期积分按本组那本账取：CodeArts（credits）的到期阶梯与其余渠道（pool）
// 同源，直接对通用字段求和即可；免费层无额度接口，恒无到期积分。
                    const hasExpiring = (group.expiring ?? 0) > 0;
                    // 次窗口只在**比主窗口多出东西**时才展示：次窗口是主窗口的超集，
                    // 相等就意味着「没有超出 1.5 天窗口的部分」。
                    const hasExpiringSecondary =
                      (group.expiringSecondary ?? 0) > (group.expiring ?? 0);
                    // 成长中心列只在支持的渠道出现（见 supportsGrowth）；**但表格列数
                    // 保持恒定**：不支持的渠道由操作列 colSpan 吃掉那一格。表格是 auto
                    // 布局，列数一变就重算列宽，展开的下一组会整列错位。
                    const showGrowth = supportsGrowth(group.provider);
                    // 列数随「是否显示操作列」与「选择列」变化：表格是 auto 布局，
                    // 列数一变就重算列宽，展开的下一组会整列错位。选择列只在可写
                    // （canWrite）时出现，与 ColumnHeaderRow / Row 的渲染条件严格一致。
                    // canWrite：选择 + 凭证 + 状态 + 额度 + token + 成长 + 操作 = 7；
                    // viewer：去掉选择与操作两列 = 5。
                    const columnCount = canWrite ? 7 : 5;
                    // 本组是否已全选：决定组头 checkbox 的勾选态。用 group.items
                    // （组内全部行）而不是 filtered，因为组头「全选本渠道」的语义
                    // 就是这一渠道下的所有账号。
                    const groupIds = group.items.map((item) => item.id);
                    const allSelected = groupIds.length > 0
                      && groupIds.every((id) => selected.has(id));
                    // 本组渠道当前能提供的模型（组头「模型 N 个」+ hover 清单）。
                    // 取不到模型列表时为空数组，那块整体不渲染。
                    const groupModels = modelsByProvider.get(group.provider) ?? [];
                    // 组头顺带给一个「最低倍率」：只看数量看不出这渠道贵不贵，
                    // 而最低倍率正是「这渠道值不值得优先用」的答案。免费额度
                    // （rate 0）不参与取最低——渠道里混着 1~2 个免费模型时按字面
                    // 取最小会显示「最低 免」，读起来像整个渠道免费。
                    const rateText = rateSummary(groupModels);
                    // 额度合计的进度条：剩余 / 总量（免费层两者皆 null，不出条）
                    const quotaPct =
                      group.remaining !== null && group.total !== null && group.total > 0
                        ? Math.min(100, Math.max(0, (group.remaining / group.total) * 100))
                        : null;
                    return (
                      <Fragment key={group.key}>
                        {/* 组头：折叠时的「渠道仪表盘」——身份 + 状态分布 + 额度合计 +
                            1.5 天 / 7 天内将过期的积分。每个指标一个带图标的图块，
                            比一行纯文字更好扫读；点击展开该渠道（默认全折叠）。 */}
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
                                      toggleGroupSelected(groupIds, checked === true)}
                                  />
                                </span>
                              )}
                              <button
                                type="button"
                                data-testid={`group-toggle-${group.key}`}
                                aria-expanded={!isCollapsed}
                                onClick={() => toggleGroup(group.key)}
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
                                  窄屏放不下时按整块换行，块内仍保持同一套对齐。 */}
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
                                    title={`组内各号主窗口（${formatDuration(expiryWindow)}）内将过期的积分合计`}
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
                                    title={`组内各号次窗口（${formatDuration(expirySecondaryWindow)}）内将过期的积分合计，含主窗口的部分`}
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
                            </div>
                          </TableCell>
                        </TableRow>
                        {!isCollapsed && (
                          <>
                            <ColumnHeaderRow
                              groupKey={group.key}
                              canWrite={canWrite}
                              showGrowth={showGrowth}
                              sort={sort}
                              order={order}
                              toggle={toggle}
                            />
                            {group.items.map((credential) => (
                              <Fragment key={credential.id}>
                                <Row
                                  credential={credential}
                                  ledger={group.ledger}
                                  now={now}
                                  expiryWindow={expiryWindow}
                                  expirySecondaryWindow={expirySecondaryWindow}
                                  tokenWarning={tokenWarning}
                                  canWrite={canWrite}
                                  showGrowth={showGrowth}
                                  busy={busy}
                                  selected={selected.has(credential.id)}
                                  onSelect={toggleSelected}
                                  confirming={confirmDelete === credential.id}
                                  creditsOpen={creditEvents?.credentialId === credential.id
                                    && creditEvents.ledger === group.ledger}
                                  onConfirmDelete={setConfirmDelete}
                                  actions={actions}
                                />
                                {/* 抽屉由调用方渲染成**独立的下一行**（见 TableBody）。 */}
                                {creditEvents?.credentialId === credential.id
                                  && creditEvents.ledger === group.ledger && (
                                  <TableRow data-testid={`credit-row-${credential.id}`}>
                                    <TableCell colSpan={columnCount} className="p-0">
                                      <CreditDrawer
                                        credentialId={credential.id}
                                        ledger={creditEvents.ledger}
                                        events={creditEvents.events}
                                        sort={creditEvents.sort}
                                        order={creditEvents.order}
                                        onSort={sortCredits}
                                        onClose={() => actions.credits(credential, creditEvents.ledger)}
                                      />
                                    </TableCell>
                                  </TableRow>
                                )}
                              </Fragment>
                            ))}
                          </>
                        )}
                      </Fragment>
                    );
                  })}
                </TableBody>
              </Table>
            )}
          </>
        )}
      </Panel>

      {canWrite && limitsCredential && (
        <CredentialLimitsDialog
          credential={limitsCredential}
          onClose={() => setLimitsFor(null)}
          onSaved={(message) => {
            setLimitsFor(null);
            showNotice(message);
            void refresh();
          }}
          onError={(message) => showError(message)}
        />
      )}

      {canWrite && tagsCredential && (
        <CredentialTagsDialog
          credential={tagsCredential}
          onClose={() => setTagsFor(null)}
          onSaved={(message) => {
            setTagsFor(null);
            showNotice(message);
            void refresh();
          }}
          onError={(message) => showError(message)}
        />
      )}

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
          // 高度只能按「弹层实际可用的空间」封顶，不能只写 70vh：弹层贴着视口
          // 上/下缘弹出时（几十个包必然超出），radix 的 flip 挪不动就只能把
          // 上半截顶到视口外。available-height 是 radix 算好的可用高度，
          // 再减掉弹层自身的内边距（py-2 上下共 1rem），否则仍差这几像素。
          className="max-h-[min(70vh,calc(var(--radix-tooltip-content-available-height)-1rem))] max-w-full overflow-x-auto overflow-y-auto"
        >
          <div className="mb-1.5 flex items-baseline justify-between gap-3 border-b border-border pb-1.5">
            <span className="text-xs font-semibold text-foreground">额度包明细</span>
            <span className="rounded-full bg-muted px-1.5 py-0.5 text-[11px] font-medium tabular-nums text-muted-foreground">
              {packages.length} 个 · 按到期先后
            </span>
          </div>
          <ul className="divide-y divide-border/60">
            {ordered.map((item, index) => (
              <li
                key={`${item.name}-${item.end}-${index}`}
                data-testid={`package-${credential.id}`}
                className="py-1 text-xs"
              >
                {item.name ? <span className="font-medium text-foreground">{item.name}：</span> : ""}
                <span className="tabular-nums">剩 {formatNumber(item.total - item.used)} / {formatNumber(item.total)}</span>
                {item.used > 0 && (
                  <span className="text-muted-foreground">（已用 {formatNumber(item.used)}）</span>
                )}
                <span className="text-muted-foreground"> · {packageExpiry(item.end)} 到期</span>
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
 * access token 到期展示。
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

/**
 * 额度占比环：SVG 圆环画「剩余 / 总量」，纯图形仪表（不塞圆心文字）。
 *
 * 用环不用条：组头已经是一个多图块的「仪表盘」，横条要占一整行宽度、还只能在
 * 数字右侧挤着；环是定宽正方，跟 28px 的图标块同高，能直接顶掉原来那个图标位。
 *
 * 圆心不放文字：28px 的环内径不到 18px，`100%` 这种四字符放进去必然截断。
 * 精确百分比交给旁边的「额度合计」标签行（11px，可读），环只负责一眼看比例。
 *
 * 用 `stroke-dasharray` 画进度而不是两段 `path`：算出弧长即可，颜色走
 * `currentColor`（父级 `text-ok`），明暗两态自动跟随 token。
 */
function QuotaRing({ pct }: { pct: number }) {
  // 半径 9、描边 3.5：外径 21.5，在 28px 的方块里留出呼吸，环身也够粗
  const radius = 9;
  const circumference = 2 * Math.PI * radius;
  const remaining = Math.min(circumference, Math.max(0, (pct / 100) * circumference));
  return (
    <span className="grid size-7 shrink-0 place-items-center text-ok">
      <svg viewBox="0 0 24 24" className="size-7 -rotate-90" aria-hidden>
        {/* 空余轨道：整圈淡色，剩余弧叠在上面 */}
        <circle
          cx="12"
          cy="12"
          r={radius}
          fill="none"
          stroke="currentColor"
          strokeOpacity={0.16}
          strokeWidth={3.5}
        />
        {/* 100% 时 dasharray 的零长段配 round 端帽会多出一个圆点，直接画整圈；
            0% 时同理会留下一个点，干脆不画进度弧。 */}
        {remaining >= circumference ? (
          <circle
            cx="12"
            cy="12"
            r={radius}
            fill="none"
            stroke="currentColor"
            strokeWidth={3.5}
          />
        ) : remaining > 0 ? (
          <circle
            cx="12"
            cy="12"
            r={radius}
            fill="none"
            stroke="currentColor"
            strokeWidth={3.5}
            strokeLinecap="round"
            strokeDasharray={`${remaining} ${circumference - remaining}`}
          />
        ) : null}
      </svg>
    </span>
  );
}

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
function ColumnHeaderRow({
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

function Row({
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
                    <DropdownMenuItem onSelect={() => actions.limits(credential)}>
                      <Gauge className="size-4" />
                      {credential.limits ? "编辑限额" : "设置限额"}
                    </DropdownMenuItem>
                    <DropdownMenuItem onSelect={() => actions.tags(credential)}>
                      <Tag className="size-4" />
                      {credential.tags.length ? "编辑标签" : "设置标签"}
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
 * 变动流水抽屉：同一行内展开，不弹层。
 *
 * 语义纪律：这里展示的是**两次额度探测之间的净变化**，不是动作归因。上游
 * 签到/成长接口不打日志，diff 看不到分数是谁加的，所以文案一律说「净变化」，
 * 只有归因已知度（observed/sync）。把净变化说成「签到获得」就是拿猜测当事实。
 *
 * `ledger` 标明读的是哪本账：`credits`＝积分账（内置组），其余＝每日池。标题
 * 与说明都按它写，否则内置组会把积分账的流水读成每日池的。
 */
function CreditDrawer({
  credentialId,
  ledger,
  events,
  sort,
  order,
  onSort,
  onClose,
}: {
  credentialId: string;
  /** 本抽屉讲哪一本账：credits＝积分账（内置组），pool＝每日池（其余） */
  ledger: Ledger;
  events: CreditEvent[];
  sort: string;
  order: string;
  onSort: (columnKey: string) => void;
  onClose: () => void;
}) {
  const creditsLedger = ledger === "credits";
  return (
    <Card
      className="mt-4 gap-0 bg-muted/30 p-3 ring-border/60"
      data-testid={`credit-drawer-${credentialId}`}
    >
      <div className="mb-2 flex items-center justify-between">
        <div className="text-sm font-medium">
          {creditsLedger ? "积分流水" : "额度流水"}
          <span className="ml-2 text-xs text-muted-foreground">
            {creditsLedger
              ? "积分账（内置模型计费）两次额度探测之间的净变化（非动作归因）"
              : "每日额度池两次额度探测之间的净变化（非动作归因）"}
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
        <Table data-testid="credit-drawer-table" cards>
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
                <TableCell label="观测时间" className="text-xs">
                  {formatTime(event.ts)}
                </TableCell>
                <TableCell label="区间起点" className="text-xs text-muted-foreground">
                  {event.window_start ? formatTime(event.window_start) : "—"}
                </TableCell>
                <TableCell
                  label="变化"
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
                <TableCell label="说明" className="text-xs text-muted-foreground">
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
