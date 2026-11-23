import { Database, Plus, Search, Upload } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { useCredentials, usePlaygroundModels, useQueryClient } from "../api/hooks";
import { groupModelsByProvider } from "../components/ModelPicker";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { PoolOverview } from "../components/PoolOverview";
import { ToastViewport, useToasts } from "../components/Toast";
import { BatchToastContent } from "./credentials/toasts";
import { useSort } from "../hooks/useSort";
import {
  ANY,
  DEFAULT_FILTERS,
  DEFAULT_GROUP_SORT,
  groupByProvider,
  matchesFilters,
  providerOptions,
  sortGroups,
  stateOptions,
  tagOptions,
} from "../lib/credentialList";
import type { GroupSort } from "../lib/credentialList";
import { errorText } from "../lib/errors";
import { useSessionContext } from "../Layout";
import { Button, EmptyState, Notice, Panel } from "../ui";
import { buildActions } from "./credentials/actions";
import type { Actions, CheckinLogState, CreditEventsState } from "./credentials/actions";
import { CredentialDialogs } from "./credentials/dialogs";
import { BatchActionBar, CredentialFilterBar } from "./credentials/filters";
import type { BatchButton } from "./credentials/helpers";
import { CredentialTable } from "./credentials/table";

/**
 * 凭证管理页：页面主体只负责取数、筛选/展开/选中状态与动作编排。
 * 组头、表格行、抽屉、筛选条、批量条与对话框都拆在 `pages/credentials/` 下，
 * 这里保留 hook 的调用顺序与请求时机不变。
 */
export function CredentialsPage() {
  const session = useSessionContext();
  // 默认 created_at 升序（与后端一致）；额度 / 健康度等数值列首点降序
  const { sort, order, toggle } = useSort("created_at", "asc",
    { health: "desc", quota_remaining: "desc",
      token_expires_at: "asc", growth_last_run_at: "desc" });
  // 30s 轮询：冷却倒计时 / token 剩余都是「随时间变化」的观测量，页面停留
  // 时应自动刷新（与 useTasks 同口径）。
  const { data, isLoading, isError, dataUpdatedAt } =
    useCredentials(session.username, 30_000, sort, order);
  // 组头「模型 N 个」：与 Playground / `/v1/models` 同源（只含有可用凭证且过
  // 当前黑名单的渠道），因此组里「有凭证」与「有模型」是同一口径，不必另算一套。
  const modelsQuery = usePlaygroundModels();
  const client = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  // 添加凭证对话框（登录渠道 / JSON 导入 / 免费层补回都收在里面）
  // `?new=1`：首启向导的深链，进来直接开新增对话框，随后把参数清掉
  const [searchParams, setSearchParams] = useSearchParams();
  const [addOpen, setAddOpen] = useState(() => searchParams.get("new") === "1");
  // 批量导入：多行 JSON / 文件，逐条校验 + 去重，失败行可回填重试
  const [batchOpen, setBatchOpen] = useState(false);
  useEffect(() => {
    if (searchParams.get("new") === "1") setSearchParams({}, { replace: true });
  }, [searchParams, setSearchParams]);
  // 限额护栏对话框：一次只编辑一条（存凭证 id，轮询替换列表后仍能取到最新值）
  const [limitsFor, setLimitsFor] = useState<string | null>(null);
  // 标签编辑对话框：同样存 id，取最新快照
  const [tagsFor, setTagsFor] = useState<string | null>(null);
  // 调度权重对话框：同样存 id，取最新快照
  const [weightFor, setWeightFor] = useState<string | null>(null);
  // Qoder 网页 Cookie 对话框：同样存 id，取最新快照
  const [webCookieFor, setWebCookieFor] = useState<string | null>(null);
  // 小浣熊账号密码对话框（按需登录用）：同样存 id，取最新快照
  const [loginSecretFor, setLoginSecretFor] = useState<string | null>(null);
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
  const [creditEvents, setCreditEvents] = useState<CreditEventsState | null>(null);
  // 签到记录抽屉：与变动流水同形（一次只开一个、行内展开）。读的是本地落库的
  // 审计日志（`checkin_events`），不触发上游请求——凭证据此能回看「上次签到
  // 几点、领了多少、上游回了什么码」，正是「显示成功却没加积分」类问题的证据。
  const [checkinLog, setCheckinLog] = useState<CheckinLogState | null>(null);

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
  // now 只在列表刷新成功（30s 轮询出结果）时推进：每次渲染取新值会让下面几个
  // useMemo 因「输入搜索词 / 勾选」等无关 state 变化而全量重算筛选与分组。
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    setNow(Date.now() / 1000);
  }, [dataUpdatedAt]);

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
  const weightCredential = useMemo(
    () => credentials.find((item) => item.id === weightFor) ?? null,
    [credentials, weightFor],
  );
  // 网页 Cookie 对话框当前编辑的凭证：同上
  const webCookieCredential = useMemo(
    () => credentials.find((item) => item.id === webCookieFor) ?? null,
    [credentials, webCookieFor],
  );
  // 账号密码对话框当前编辑的凭证：同上
  const loginSecretCredential = useMemo(
    () => credentials.find((item) => item.id === loginSecretFor) ?? null,
    [credentials, loginSecretFor],
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
  // 连点列头会并发多个请求：只认最新一次的结果，先发后到的旧响应必须丢弃，
  // 否则它会把刚点出来的排序覆盖回去（`sort`/`order` 与表格内容都会退化）。
  const creditSortSeq = useRef(0);
  const sortCredits = async (columnKey: string) => {
    const target = creditEvents;
    if (!target) return;
    const order = target.sort === columnKey
      ? (target.order === "asc" ? "desc" : "asc")
      : (CREDIT_FIRST_ORDER[columnKey] ?? "asc");
    const seq = ++creditSortSeq.current;
    try {
      const result = await api.creditEvents(
        target.credentialId, target.ledger, columnKey, order);
      if (seq !== creditSortSeq.current) return;
      // 函数式更新：抽屉可能已被关掉或切到别的账本，闭包里的旧快照不能写回去
      setCreditEvents((previous) => (previous
        ? { ...previous, events: result.events, sort: columnKey, order }
        : previous));
    } catch (caught) {
      if (seq === creditSortSeq.current) showError(errorText(caught, "流水读取失败"));
    }
  };

  const run = async (task: () => Promise<unknown>, successMessage?: string) => {
    setBusy(true);
    try {
      await task();
      if (successMessage) showNotice(successMessage);
      await refresh();
    } catch (caught) {
      showError(errorText(caught, "操作失败"));
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
      showError(errorText(caught, "批量操作失败"));
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

  // 动作表在 `credentials/actions.ts` 里构造：这里把页面持有的状态与回调
  // 显式传进去，动作实现与文案原样不变。
  const actions: Actions = buildActions({
    run,
    refresh,
    push,
    showError,
    showNotice,
    setBusy,
    setConfirmDelete,
    creditEvents,
    setCreditEvents,
    checkinLog,
    setCheckinLog,
    setLimitsFor,
    setTagsFor,
    setWeightFor,
    setWebCookieFor,
    setLoginSecretFor,
  });

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
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              variant="ghost"
              data-testid="open-batch-import"
              disabled={busy}
              onClick={() => setBatchOpen(true)}
            >
              <Upload className="size-4" /> 批量导入
            </Button>
            <Button
              size="sm"
              variant="primary"
              data-testid="open-add-dialog"
              disabled={busy}
              onClick={() => setAddOpen(true)}
            >
              <Plus className="size-4" /> 添加凭证
            </Button>
          </div>
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
            <CredentialFilterBar
              filters={filters}
              total={credentials.length}
              providerCounts={providerCounts}
              stateCounts={stateCounts}
              tagCounts={tagCounts}
              groupSort={groupSort}
              filteredCount={filtered.length}
              filtering={filtering}
              onFiltersChange={applyFilters}
              onGroupSortChange={setGroupSort}
            />

            {/* 批量操作条：只在有选中项时出现，不占常驻高度。选中集合与当前
                筛选结果求交（selectedIds），因此「筛选后勾选、再改筛选」不会
                把已隐藏的行带进批量动作。 */}
            {canWrite && selectedIds.length > 0 && (
              <BatchActionBar
                selectedCount={selectedIds.length}
                busy={busy}
                onRun={(button) => void runBatch(button)}
                onClear={() => setSelected(new Set())}
              />
            )}

            {filtered.length === 0 ? (
              <EmptyState
                icon={<Search className="size-5" />}
                title="没有匹配的凭证"
                description="换个关键词或清空筛选查看全部。"
                data-testid="no-credential-match"
              />
            ) : (
              <CredentialTable
                groups={groups}
                expanded={expanded}
                canWrite={canWrite}
                selected={selected}
                now={now}
                expiryWindow={expiryWindow}
                expirySecondaryWindow={expirySecondaryWindow}
                tokenWarning={tokenWarning}
                busy={busy}
                confirmDelete={confirmDelete}
                setConfirmDelete={setConfirmDelete}
                actions={actions}
                creditEvents={creditEvents}
                checkinLog={checkinLog}
                sortCredits={(columnKey) => void sortCredits(columnKey)}
                toggleSelected={toggleSelected}
                toggleGroup={toggleGroup}
                toggleGroupSelected={toggleGroupSelected}
                modelsByProvider={modelsByProvider}
                sort={sort}
                order={order}
                toggle={toggle}
              />
            )}
          </>
        )}
      </Panel>

      <CredentialDialogs
        canWrite={canWrite}
        limitsCredential={limitsCredential}
        setLimitsFor={setLimitsFor}
        tagsCredential={tagsCredential}
        setTagsFor={setTagsFor}
        weightCredential={weightCredential}
        setWeightFor={setWeightFor}
        loginSecretCredential={loginSecretCredential}
        setLoginSecretFor={setLoginSecretFor}
        webCookieCredential={webCookieCredential}
        setWebCookieFor={setWebCookieFor}
        batchOpen={batchOpen}
        setBatchOpen={setBatchOpen}
        addOpen={addOpen}
        setAddOpen={setAddOpen}
        credentialCount={credentials.length}
        hasZen={hasZen}
        hasKilo={hasKilo}
        lingxiCount={credentials.filter((row) => row.provider === "lingxi").length}
        tosRisks={data?.tos_risks ?? {}}
        showNotice={showNotice}
        showError={showError}
        refresh={refresh}
      />
    </div>
  );
}
