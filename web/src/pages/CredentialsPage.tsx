import { useState } from "react";
import {
  CalendarCheck,
  Database,
  HeartPulse,
  MoreHorizontal,
  Pin,
  Power,
  RefreshCw,
  Sparkles,
  Trash2,
} from "lucide-react";
import { api } from "../api/client";
import { useSessionContext } from "../Layout";
import { useCredentials, useQueryClient } from "../api/hooks";
import { HelpBlock } from "../components/HelpBlock";
import { PageHeader } from "../components/PageHeader";
import { ProviderIcon } from "../components/ProviderIcon";
import { ColumnHint } from "../components/Tip";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  credentialState,
  cooldownRemaining,
  expiringQuotaLabel,
  formatDuration,
  formatNumber,
  formatTime,
  healthView,
  probeFailureLabel,
  quotaSemantics,
  STATE_LABEL,
  STATE_TONE,
} from "../api/display";
import type { Credential, GrowthRunResult, Provider } from "../api/types";
import {
  Badge,
  Button,
  Empty,
  Field,
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
  Textarea,
} from "../ui";

const PROVIDERS: Provider[] = ["codebuddy", "trae"];
const PROVIDER_LABEL: Record<Provider, string> = { codebuddy: "CodeBuddy", trae: "TRAE" };

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

interface Actions {
  revive: (credential: Credential) => void;
  toggle: (credential: Credential) => void;
  pin: (credential: Credential) => void;
  remove: (credential: Credential) => void;
  probe: (credential: Credential) => void;
  checkin: (credential: Credential) => void;
  growth: (credential: Credential) => void;
}

export function CredentialsPage() {
  const session = useSessionContext();
  const { data, isLoading } = useCredentials(session.username);
  const client = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  // 每个渠道各自可能有进行中的登录（CodeBuddy 轮询 / TRAE 回调）
  const [loginProviders, setLoginProviders] = useState<Provider[]>([]);
  const [probeDetail, setProbeDetail] = useState<string | null>(null);
  // 成长中心最近一轮：展示逐步结果（一句话汇报看不出哪一步没做成）
  const [growthResult, setGrowthResult] = useState<GrowthRunResult | null>(null);

  const credentials = data?.credentials ?? [];
  const expiryWindow = data?.expiry_window_seconds ?? 0;
  const isAdmin = data?.is_admin ?? false;
  const now = Date.now() / 1000;

  const refresh = () => client.invalidateQueries({ queryKey: ["admin"] });

  const run = async (task: () => Promise<unknown>, successMessage?: string) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await task();
      if (successMessage) setNotice(successMessage);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "操作失败");
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
        credential.enabled === 1 ? "已停用该凭证。" : "已启用该凭证。",
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
        setError(null);
        setNotice(null);
        setProbeDetail(null);
        try {
          const result = await api.probeCredential(credential.id);
          // 探测失败表示「未知」，绝不能显示成额度为 0。
          // 失败原因用可操作的中文说明，不直接把后端枚举或异常类名丢给用户。
          if (result.probed) {
            setNotice(
              `探测成功：剩余 ${formatNumber(result.remaining)} / ${formatNumber(result.total)}`,
            );
          } else {
            setNotice(
              `探测失败：${probeFailureLabel(result.reason)}（健康度保持为「未探测」）`,
            );
            if (result.detail) setProbeDetail(result.detail);
          }
          await refresh();
        } catch (caught) {
          setError(caught instanceof Error ? caught.message : "探测失败");
        } finally {
          setBusy(false);
        }
      })(),
    checkin: (credential) =>
      void (async () => {
        setBusy(true);
        setError(null);
        setNotice(null);
        try {
          const result = await api.checkinCredential(credential.id);
          // 渠道可选回填的活动状态：连续天数/今日积分（TRAE 为 null，拼接自动跳过）
          const streak = result.status?.streak_days ?? null;
          const tail = streak === null ? "" : `（连续 ${streak} 天）`;
          if (result.ok && result.already_checked_in) {
            // 渠道把「已签到」返回成 HTTP 400 + code=10001，这不是错误
            setNotice(`${result.message || "今天已签到，请明天再来"}${tail}`);
          } else if (result.ok) {
            setNotice(
              (result.credit === null
                ? "签到成功"
                : `签到成功，获得 ${formatNumber(result.credit)} 积分`) + tail,
            );
          } else {
            setNotice(
              `签到未成功（code=${result.code ?? "null"}）${result.message ? ` ${result.message}` : ""}`,
            );
          }
          await refresh();
        } catch (caught) {
          setError(caught instanceof Error ? caught.message : "签到失败");
        } finally {
          setBusy(false);
        }
      })(),
    growth: (credential) =>
      void (async () => {
        setBusy(true);
        setError(null);
        setNotice(null);
        try {
          const result = await api.runGrowth(credential.id);
          setGrowthResult(result);
          setNotice(growthNotice(result));
          await refresh();
        } catch (caught) {
          setError(caught instanceof Error ? caught.message : "成长中心执行失败");
        } finally {
          setBusy(false);
        }
      })(),
  };

  const startLogin = async (provider: Provider) => {
    setError(null);
    setNotice(null);
    // 先同步开一个占位窗口：window.open 若在 await 之后才调用，
    // 会脱离用户手势上下文而被浏览器弹窗拦截。
    const popup = window.open("", "_blank");
    try {
      const started = await api.upstreamStart(provider);
      if (started.auth_url && popup && !popup.closed) {
        popup.location.href = started.auth_url;
      } else if (popup) {
        popup.close();                         // 失败时关掉空白占位窗
      }
      setLoginProviders((previous) => [...new Set([...previous, provider])]);

      if (started.flow === "callback") {
        // TRAE：浏览器完成授权后 302 回本服务的 /authorize，那里直接落库。
        // 前端无法轮询渠道，改为轮询凭证列表，出现新凭证即视为完成。
        setNotice("已在新标签页打开授权页。完成授权后本页会自动刷新出凭证。");
        const deadline = Date.now() + 5 * 60 * 1000;
        const baseline = credentials.length;
        const timer = window.setInterval(async () => {
          const after = (await api.credentials()).credentials.length;
          if (after > baseline || Date.now() > deadline) {
            window.clearInterval(timer);
            setLoginProviders((previous) => previous.filter((item) => item !== provider));
            setNotice(after > baseline ? "登录成功，凭证已保存。" : "授权超时，请重新发起登录。");
          }
        }, 3000);
        return;
      }

      setNotice("已在新标签页打开授权页，完成后此页会自动检测。");
      const interval = (started.interval ?? 5) * 1000;
      const timer = window.setInterval(async () => {
        try {
          const result = await api.upstreamPoll(provider, started.state);
          if (result.status === "success") {
            window.clearInterval(timer);
            setLoginProviders((previous) => previous.filter((item) => item !== provider));
            setNotice("登录成功，凭证已保存。");
            await refresh();
          }
        } catch {
          window.clearInterval(timer);
          setLoginProviders((previous) => previous.filter((item) => item !== provider));
          setError("登录轮询失败，请重试。");
        }
      }, interval);
    } catch (caught) {
      // 启动失败：关掉占位窗口，并把授权地址给出来让用户手动打开
      popup?.close();
      setError(caught instanceof Error ? caught.message : "无法启动登录流程");
    }
  };

  const cancelLogin = async (provider: Provider) => {
    setError(null);
    try {
      const started = await api.upstreamStart(provider).catch(() => null);
      if (started) await api.upstreamCancel(provider, started.state).catch(() => undefined);
    } finally {
      setLoginProviders((previous) => previous.filter((item) => item !== provider));
      setNotice("已取消登录。");
    }
  };

  if (isLoading) return <Empty>载入中…</Empty>;

  return (
    <div className="space-y-6" data-testid="credentials-page">
      <PageHeader
        title="凭证管理"
        description="凭证是调度池里可被选中的渠道账号。登录渠道授权或粘贴 JSON 导入后，可在此探测剩余额度、签到或启停。"
        icon={<Database className="size-5" />}
      />
      {!isAdmin && (
        <div data-testid="readonly-banner">
          <Notice tone="muted">只读模式：仅管理员可以导入、启停或删除凭证。</Notice>
        </div>
      )}
      {credentials.length === 0 && isAdmin && (
        <div data-testid="first-run-hint">
          <Notice tone="muted">
            还没有凭证。用下方「登录渠道账号」完成 CodeBuddy / TRAE 授权，或直接粘贴凭证 JSON 导入；
            凭证表下方的「凭证状态与操作说明」可查看各状态和按钮的含义。
          </Notice>
        </div>
      )}
      {error && (
        <div data-testid="credentials-error">
          <Notice tone="danger">{error}</Notice>
        </div>
      )}
      {notice && (
        <div data-testid="credentials-notice">
          <Notice tone="ok">
            {notice}
            {probeDetail && (
              <span className="ml-2 text-muted-foreground" data-testid="probe-detail">
                原始错误：{probeDetail}
              </span>
            )}
          </Notice>
        </div>
      )}
      {growthResult && (
        <div data-testid="growth-result">
          <Notice tone={growthResult.ok ? "ok" : "danger"}>
            <div className="font-medium">成长中心：{growthResult.report}</div>
            <ul className="mt-1 space-y-0.5 text-xs text-muted-foreground">
              {growthResult.steps.map((step, index) => (
                <li key={`${step.name}-${index}`} data-testid="growth-step">
                  <span className={GROWTH_STEP_TONE[step.status]}>
                    {GROWTH_STEP_LABEL[step.status]}
                  </span>
                  {step.name}
                  {step.detail && `：${step.detail}`}
                </li>
              ))}
            </ul>
            {growthResult.session_dead && (
              <div className="mt-1 text-xs">
                登录态已失效，需重新登录该渠道后才能继续领取。
              </div>
            )}
          </Notice>
        </div>
      )}

      <Panel title="凭证池">
        {credentials.length === 0 ? (
          <Empty data-testid="no-credentials">还没有凭证</Empty>
        ) : (
          <Table data-testid="credentials-table">
            <TableHeader>
              <TableRow>
                <TableHead>昵称</TableHead>
                <TableHead>渠道</TableHead>
                <TableHead><span className="inline-flex items-center gap-1">状态<ColumnHint text="可用/冷却中/已禁用/已关闭/额度耗尽；冷却中到期自动恢复。" /></span></TableHead>
                <TableHead><span className="inline-flex items-center gap-1">健康度<ColumnHint text="剩余积分占比三态：已知百分比 / 未探测 / 已耗尽。未探测≠已耗尽，点「探测」可重试。" /></span></TableHead>
                <TableHead><span className="inline-flex items-center gap-1">额度<ColumnHint text="CodeBuddy 本周期剩余按日期重置；TRAE 账户剩余单调递减。到期积分行＝调度窗口内即将过期、会被优先消耗的额度。" /></span></TableHead>
                <TableHead><span className="inline-flex items-center gap-1">成长中心<ColumnHint text="仅 CodeBuddy：最近一轮成长中心（旅行礼物/任务/连登兑换/盲盒）的领取结果与时间，由定时任务或手动执行写入。" /></span></TableHead>
                {isAdmin && <TableHead className="text-right"><span className="inline-flex items-center gap-1">操作<ColumnHint text="探测：查剩余额度；签到：领当日积分；指定：设为优先；停用/删除：移出调度或移除。" /></span></TableHead>}
              </TableRow>
            </TableHeader>
            <TableBody>
              {credentials.map((credential) => (
                <Row
                  key={credential.id}
                  credential={credential}
                  now={now}
                  expiryWindow={expiryWindow}
                  isAdmin={isAdmin}
                  busy={busy}
                  confirming={confirmDelete === credential.id}
                  onConfirmDelete={setConfirmDelete}
                  actions={actions}
                />
              ))}
            </TableBody>
          </Table>
        )}
      </Panel>

      {isAdmin && (
        <div className="grid gap-6 lg:grid-cols-2" data-testid="add-credentials">
          <Panel title="登录渠道账号">
            <div className="flex flex-wrap items-center gap-3">
              {PROVIDERS.map((item) => {
                const pending = loginProviders.includes(item);
                return pending ? (
                  <span key={item} className="inline-flex items-center gap-2">
                    <span className="text-xs text-muted-foreground">
                      {PROVIDER_LABEL[item]} 登录中…
                    </span>
                    <Button
                      size="sm"
                      variant="danger"
                      data-testid={`cancel-login-${item}`}
                      onClick={() => void cancelLogin(item)}
                    >
                      取消登录
                    </Button>
                  </span>
                ) : (
                  <Button
                    key={item}
                    size="sm"
                    variant="primary"
                    data-testid={`start-login-${item}`}
                    disabled={busy}
                    onClick={() => void startLogin(item)}
                  >
                    登录 {PROVIDER_LABEL[item]}
                  </Button>
                );
              })}
            </div>
            <p className="mt-2 text-xs text-muted-foreground">
              CodeBuddy 走设备码轮询（本页自动轮询渠道）；TRAE 走浏览器回调
              （授权后由 <code>/authorize</code> 直接落库，本页轮询凭证列表检测完成）。
              也可以直接粘贴凭证 JSON 导入。
            </p>
          </Panel>
          <ImportPanel
            busy={busy}
            onImport={async (provider, credential, nickname) => {
              await run(() => api.importCredential(provider, credential, nickname), "凭证已导入。");
            }}
          />
        </div>
      )}

      <HelpBlock
        title="凭证状态与操作说明"
        entries={[
          { term: "可用", where: "状态列", meaning: "该凭证当前能被调度器选中处理请求。" },
          { term: "冷却中（显示剩余时间）", where: "状态列", meaning: "渠道暂时拒绝（权益耗尽 12 小时、限流 60 秒、连续出错 10 分钟），到期自动恢复，无需手动操作。" },
          { term: "已禁用", where: "状态列", meaning: "渠道判定会话失效，凭证已永久停止使用；删除后重新登录该账号即可。" },
          { term: "已关闭", where: "状态列", meaning: "管理员手动停用（软开关），随时可以重新启用。" },
          { term: "健康度：百分比", where: "健康度列", meaning: "剩余积分占总积分的比例，调度器优先选数值高的。" },
          { term: "健康度：未探测到额度", where: "健康度列", meaning: "探测失败或渠道没返回额度信息。注意它不是「已耗尽」——点「探测」可重新获取。" },
          { term: "额度下方的时间语义", where: "额度列", meaning: "CodeBuddy 是「本周期剩余，<日期> 重置」；TRAE 是「账户剩余（单调递减）」。两者单位都是积分，但重置行为不同。" },
          { term: "探测 / 签到", where: "操作列", meaning: "探测：立即向渠道查询一次剩余额度。签到：领取当日积分（每天 9 点系统自动签到）。" },
          { term: "指定 / 停用 / 删除", where: "操作列", meaning: "指定：把该凭证设为优先使用的唯一凭证（全局只能指定一个）。停用：临时移出调度池不删数据。删除：彻底移除凭证。" },
        ]}
      />

    </div>
  );
}

function Row({
  credential,
  now,
  expiryWindow,
  isAdmin,
  busy,
  confirming,
  onConfirmDelete,
  actions,
}: {
  credential: Credential;
  now: number;
  expiryWindow: number;
  isAdmin: boolean;
  busy: boolean;
  confirming: boolean;
  onConfirmDelete: (id: string | null) => void;
  actions: Actions;
}) {
  const expiring = expiringQuotaLabel(credential.quota_expiring_credits, expiryWindow);
  const health = healthView(credential.health);
  const state = credentialState(credential, now);
  const cooldown = cooldownRemaining(credential.cooling_until, now);

  return (
    <TableRow data-testid={`row-${credential.id}`}>
      <TableCell>
        {credential.nickname || credential.id.slice(0, 12)}
        {credential.pinned === 1 && (
          <span className="ml-2">
            <Badge tone="accent">已指定</Badge>
          </span>
        )}
      </TableCell>
      <TableCell className="text-xs">
        <span className="inline-flex items-center gap-1.5">
          <ProviderIcon provider={credential.provider} size={13} />
          {PROVIDER_LABEL[credential.provider]}
        </span>
      </TableCell>
      <TableCell>
        <Badge tone={STATE_TONE[state]}>{STATE_LABEL[state]}</Badge>
        {state === "cooling" && (
          <span className="ml-2 text-xs text-muted-foreground">{formatDuration(cooldown)}</span>
        )}
        {credential.disabled_reason && state === "disabled" && (
          <span className="ml-2 text-xs text-muted-foreground">
            {credential.disabled_reason}
          </span>
        )}
      </TableCell>
      <TableCell>
        <Badge tone={health.tone}>{health.label}</Badge>
        {health.kind === "unknown" && (
          <span className="ml-2 text-xs text-muted-foreground">探测失败或未提供</span>
        )}
      </TableCell>
      <TableCell className="text-xs">
        {formatNumber(credential.quota_remaining)} / {formatNumber(credential.quota_total)}
        {expiring && (
          <div className="text-warn" data-testid="quota-expiring">
            {expiring}
          </div>
        )}
        <div className="text-muted-foreground">{quotaSemantics(credential)}</div>
        <div className="text-muted-foreground">
          探测于 {formatTime(credential.quota_probed_at)}
        </div>
      </TableCell>
      <TableCell className="text-xs text-muted-foreground">
        {credential.growth_last_result ? (
          <div data-testid={`growth-${credential.id}`}>
            {credential.growth_last_result}
            <div className="text-muted-foreground">
              {formatTime(credential.growth_last_run_at)}
            </div>
          </div>
        ) : (
          <span>—</span>
        )}
      </TableCell>
      {isAdmin && (
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
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    size="icon"
                    variant="ghost"
                    aria-label="操作"
                    disabled={busy}
                    data-testid={`actions-${credential.id}`}
                  >
                    <MoreHorizontal />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-44">
                  <DropdownMenuItem onSelect={() => actions.probe(credential)}>
                    <RefreshCw className="size-4" /> 探测
                  </DropdownMenuItem>
                  <DropdownMenuItem onSelect={() => actions.checkin(credential)}>
                    <CalendarCheck className="size-4" /> 签到
                  </DropdownMenuItem>
                  {credential.provider === "codebuddy" && (
                    <DropdownMenuItem onSelect={() => actions.growth(credential)}>
                      <Sparkles className="size-4" /> 成长中心
                    </DropdownMenuItem>
                  )}
                  <DropdownMenuSeparator />
                  {credential.disabled === 1 && (
                    <DropdownMenuItem onSelect={() => actions.revive(credential)}>
                      <HeartPulse className="size-4" /> 恢复
                    </DropdownMenuItem>
                  )}
                  <DropdownMenuItem onSelect={() => actions.toggle(credential)}>
                    <Power className="size-4" /> {credential.enabled === 1 ? "停用" : "启用"}
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
            )}
          </div>
        </TableCell>
      )}
    </TableRow>
  );
}

function ImportPanel({
  busy,
  onImport,
}: {
  busy: boolean;
  onImport: (provider: Provider, credential: unknown, nickname: string) => Promise<void>;
}) {
  const [provider, setProvider] = useState<Provider>("codebuddy");
  const [nickname, setNickname] = useState("");
  const [raw, setRaw] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setLocalError(null);
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      setLocalError('凭证必须是合法 JSON。CodeBuddy 可填 {"token":"..."}，TRAE 填完整凭证对象。');
      return;
    }
    await onImport(provider, parsed, nickname);
    setRaw("");
    setNickname("");
  };

  return (
    <Panel title="导入凭证">
      <form onSubmit={submit} className="space-y-3">
        <div className="flex flex-wrap gap-3">
          <div className="w-40">
            <Field label="渠道">
              <Select
                value={provider}
                data-testid="import-provider"
                onChange={(event) => setProvider(event.target.value as Provider)}
              >
                {PROVIDERS.map((item) => (
                  <option key={item} value={item}>
                    {PROVIDER_LABEL[item]}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
          <div className="w-48">
            <Field label="昵称（可选）">
              <Input
                value={nickname}
                data-testid="import-nickname"
                onChange={(event) => setNickname(event.target.value)}
              />
            </Field>
          </div>
        </div>
        <Field
          label="凭证 JSON"
          hint='CodeBuddy：{"token":"..."}（也接受 access_token / bearer_token）；TRAE：{"accessToken":"...","uid":"...","refreshToken":"..."}'
        >
          <Textarea
            rows={4}
            value={raw}
            data-testid="import-payload"
            className="font-mono text-xs"
            placeholder='{"token":"..."}'
            onChange={(event) => setRaw(event.target.value)}
          />
        </Field>
        {localError && <Notice tone="danger">{localError}</Notice>}
        <Button type="submit" variant="primary" disabled={busy} data-testid="import-submit">
          导入
        </Button>
      </form>
    </Panel>
  );
}
