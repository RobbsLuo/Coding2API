import { Fragment, useState } from "react";
import {
  CalendarCheck,
  Database,
  HeartPulse,
  History,
  MoreHorizontal,
  Pin,
  Power,
  Radio,
  RefreshCw,
  Sparkles,
  Trash2,
} from "lucide-react";
import { api } from "../api/client";
import { useSessionContext } from "../Layout";
import { useCredentials, useQueryClient } from "../api/hooks";
import { HelpBlock } from "../components/HelpBlock";
import { ColumnHint, LongTextTip } from "../components/Tip";
import { PageHeader } from "../components/PageHeader";
import { ProviderIcon } from "../components/ProviderIcon";
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
  healthView,
  modelCooldownLabel,
  probeFailureLabel,
  quotaSemantics,
  STATE_LABEL,
  STATE_TONE,
  tokenExpiryView,
} from "../api/display";
import type { Credential, CreditEvent, GrowthRunResult, Provider } from "../api/types";
import type { TokenExpiryView } from "../api/display";
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
  // 积分记录抽屉：一次只开一个（同一行内展开，不弹层——
  // 表格行本身就是最好的上下文，弹层会遮掉额度列）
  const [creditEvents, setCreditEvents] = useState<{
    credentialId: string;
    events: CreditEvent[];
  } | null>(null);

  const credentials = data?.credentials ?? [];
  const expiryWindow = data?.expiry_window_seconds ?? 0;
  const expirySecondaryWindow = data?.expiry_secondary_window_seconds ?? 0;
  const tokenWarning = data?.token_expiry_warning_seconds ?? 0;
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
    // 只读：开关抽屉不写任何状态，因此不复用 run()（那个会清 notice 并刷新列表）
    credits: (credential) =>
      void (async () => {
        if (creditEvents?.credentialId === credential.id) {
          setCreditEvents(null);
          return;
        }
        setError(null);
        try {
          const result = await api.creditEvents(credential.id);
          setCreditEvents({ credentialId: credential.id, events: result.events });
        } catch (caught) {
          setError(caught instanceof Error ? caught.message : "积分记录读取失败");
        }
      })(),
    activity: (credential) =>
      void (async () => {
        setBusy(true);
        setError(null);
        setNotice(null);
        try {
          const result = await api.reportActivity(credential.id);
          setNotice(result.ok
            ? "活跃上报成功（已补发一条对话事件）"
            : `活跃上报失败：${result.message || "未知原因"}`);
          await refresh();
        } catch (caught) {
          setError(caught instanceof Error ? caught.message : "活跃上报失败");
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
                <TableHead><span className="inline-flex items-center gap-1">状态<ColumnHint text="可用/冷却中/已禁用/已暂停/额度耗尽；已暂停只摘对话流量，签到/刷新/探测照常；冷却中到期自动恢复。" /></span></TableHead>
                <TableHead><span className="inline-flex items-center gap-1">健康度<ColumnHint text="剩余积分占比三态：已知百分比 / 未探测 / 已耗尽。未探测≠已耗尽，点「探测」可重试。" /></span></TableHead>
                <TableHead><span className="inline-flex items-center gap-1">额度<ColumnHint text="CodeBuddy 本周期剩余按日期重置；TRAE 账户剩余单调递减。到期积分行＝调度窗口内即将过期、会被优先消耗的额度；主窗口（36h）打平时才比较次窗口（7 天）。" /></span></TableHead>
                <TableHead><span className="inline-flex items-center gap-1">成长中心<ColumnHint text="仅 CodeBuddy：最近一轮成长中心（旅行礼物/任务/连登兑换/盲盒）的领取结果与时间，由定时任务或手动执行写入。" /></span></TableHead>
                {isAdmin && <TableHead className="text-right"><span className="inline-flex items-center gap-1">操作<ColumnHint text="探测：查剩余额度；签到：领当日积分；指定：设为优先；暂停/删除：摘对话流量或移除。" /></span></TableHead>}
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
                    isAdmin={isAdmin}
                    busy={busy}
                    confirming={confirmDelete === credential.id}
                    onConfirmDelete={setConfirmDelete}
                    actions={actions}
                  />
                  {creditEvents?.credentialId === credential.id && (
                    <TableRow data-testid={`credit-row-${credential.id}`}>
                      <TableCell colSpan={isAdmin ? 7 : 6} className="p-0">
                        <CreditDrawer
                          credentialId={credential.id}
                          events={creditEvents.events}
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
          { term: "已暂停", where: "状态列", meaning: "管理员手动暂停（软开关）：只把该凭证摘出对话流量，签到 / token 刷新 / 成长中心 / 额度探测照常运行；随时可以取消暂停。" },
          { term: "健康度：百分比", where: "健康度列", meaning: "剩余积分占总积分的比例，调度器优先选数值高的。" },
          { term: "健康度：未探测到额度", where: "健康度列", meaning: "探测失败或渠道没返回额度信息。注意它不是「已耗尽」——点「探测」可重新获取。" },
          { term: "额度下方的时间语义", where: "额度列", meaning: "CodeBuddy 是「本周期剩余，<日期> 重置」；TRAE 是「账户剩余（单调递减）」。两者单位都是积分，但重置行为不同。" },
          { term: "到期积分（两行）", where: "额度列", meaning: "选号先比 36 小时内会过期的积分，多的先用；都相同（常见的是都为 0）时再比 7 天内会过期的积分。主窗口已有数字就只显示那一行，次窗口只在主窗口为空时才出现。" },
          { term: "套餐 N 个（额度首行右侧）", where: "额度列", meaning: "该账号当前生效的额度包个数。鼠标悬浮看每个包的名字、剩余/总量、已用与到期日（按到期先后）。未探测或渠道未返回明细时不显示。" },
          { term: "探测 / 签到", where: "操作列", meaning: "探测：立即向渠道查询一次剩余额度。签到：领取当日积分（每天 9 点系统自动签到）。" },
          { term: "指定 / 暂停 / 删除", where: "操作列", meaning: "指定：把该凭证设为优先使用的唯一凭证（全局只能指定一个）。暂停：只摘出对话流量不删数据（签到 / 刷新 / 探测不受影响）。删除：彻底移除凭证。" },
          { term: "模型避让（额度列下方）", where: "额度列", meaning: "某个模型在该账号上限流（6004）或该账号无此模型（11102）时只避让这一个模型——整条凭证仍参与调度，换其他模型立刻可用。模型限流按 10 分钟起指数退避，最长 2 小时；「无此模型」按 6 小时起，最长 24 小时。" },
          { term: "成长中心 / 活跃上报", where: "操作列", meaning: "成长中心：手动跑一轮成长中心领取（与定时任务同一条路径）。活跃上报：手动补发一条对话事件以续上「连登天数」——默认关闭的定时任务不做，这里仅供部署后验证；官方条款禁止脚本篡改活动数据，开启/使用前请自行评估账号风险。" },
        ]}
      />

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
      <button
        type="button"
        className="cursor-help text-left underline decoration-dotted underline-offset-2"
        data-testid={`packages-toggle-${credential.id}`}
      >
        套餐 {packages.length} 个
      </button>
    </LongTextTip>
  );
}
/** 生效中的模型级冷却（6004 模型限流 / 11102 该账号无此模型）。
 *
 * 与账号级「冷却中」状态是两回事：整条凭证仍可用，只是这些模型被避开。
 * 不展示会让人以为模型挂了或在池子里乱试。
 */
function ModelCooldownList({ credential, now }: { credential: Credential; now: number }) {
  const items = activeModelCooldowns(credential, now);
  if (items.length === 0) return null;
  return (
    <div className="text-warn" data-testid={`model-cooldowns-${credential.id}`}>
      {items.map((item) => (
        <div key={item.model}>
          模型 {item.model}：{modelCooldownLabel(item)}，
          {formatDuration(Math.round(item.cooling_until - now))}后重试
          {item.hits > 1 && `（连续 ${item.hits} 次）`}
        </div>
      ))}
    </div>
  );
}

/**
 * access token 到期展示（B3.3）。
 *
 * 为什么同时给「剩余」和「最后续期」：只剩 3 天看起来像快挂了，但如果两分钟前
 * 刚续期过，那其实是刚拿到的 30 天里剩下的部分；反过来只剩 3 天且最后续期是
 * 十天前，才是真的没人管。单看剩余天数会把这两种情况读反，两个一起给才读得对。
 *
 * 进度条量纲是 token 自己的寿命（`exp - iat`）；拿不到 iat 时不画条，只给数字。
 * 到期时间未知（后端 0）时整块不渲染：渠道没给到期信息不等于马上过期。
 */
function TokenExpiry({
  credential,
  view,
}: {
  credential: Credential;
  view: TokenExpiryView;
}) {
  if (view.remaining === null) return null;
  const barTone = view.tone === "danger" ? "bg-destructive"
    : view.tone === "warn" ? "bg-warn"
    : "bg-ok";
  return (
    <div className="mt-1" data-testid={`token-expiry-${credential.id}`}>
      {view.percent !== null && (
        <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
          <div
            className={`h-full rounded-full transition-all ${barTone}`}
            style={{ width: `${view.percent}%` }}
            data-testid={`token-expiry-bar-${credential.id}`}
          />
        </div>
      )}
      <div className={view.expiring ? "text-destructive" : "text-muted-foreground"}>
        token {view.remaining <= 0 ? "已过期" : `剩余 ${view.label}`}
        {view.expiring && view.remaining > 0 && "，即将到期"}
        {credential.token_issued_at > 0 &&
          ` · 最后续期 ${formatTime(credential.token_issued_at)}`}
      </div>
    </div>
  );
}

function Row({
  credential,
  now,
  expiryWindow,
  expirySecondaryWindow,
  tokenWarning,
  isAdmin,
  busy,
  confirming,
  onConfirmDelete,
  actions,
}: {
  credential: Credential;
  now: number;
  expiryWindow: number;
  expirySecondaryWindow: number;
  tokenWarning: number;
  isAdmin: boolean;
  busy: boolean;
  confirming: boolean;
  onConfirmDelete: (id: string | null) => void;
  actions: Actions;
}) {
  const expiring = expiringQuotaLabel(credential.quota_expiring_credits, expiryWindow);
  // 次窗口只在主窗口没有可展示内容时才渲染：7 天窗口是 36h 的超集，
  // 主窗口已有数字时再列一行只会重复。它的作用是解释「36h 内没有
  // 到期积分、但一周内会过期」的账号为何仍被优先选中。
  const expiringSecondary = expiring
    ? null
    : expiringQuotaLabel(
        credential.quota_expiring_credits_secondary,
        expirySecondaryWindow,
        "secondary",
      );
  const health = healthView(credential.health);
  const state = credentialState(credential, now);
  const cooldown = cooldownRemaining(credential.cooling_until, now);
  const tokenExpiry = tokenExpiryView(
    credential.token_expires_at, credential.token_issued_at, tokenWarning, now);

  // 抽屉由调用方渲染成**独立的下一行**（见 TableBody）：塞进本行的最后一个单元格
  // 只会挤在「操作」列里——同行内的 colSpan 不生效，整行高度会被拉坏。
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
        <div className="flex flex-wrap items-baseline gap-x-2">
          <span>
            {formatNumber(credential.quota_remaining)} / {formatNumber(credential.quota_total)}
          </span>
          <PackageLadder credential={credential} />
        </div>
        {expiring && (
          <div className="text-warn" data-testid="quota-expiring">
            {expiring}
          </div>
        )}
        {expiringSecondary && (
          <div className="text-warn/80" data-testid="quota-expiring-secondary">
            {expiringSecondary}
          </div>
        )}
        <div className="text-muted-foreground">{quotaSemantics(credential)}</div>
        <ModelCooldownList credential={credential} now={now} />
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
                  <DropdownMenuItem onSelect={() => actions.credits(credential)}>
                    <History className="size-4" /> 积分记录
                  </DropdownMenuItem>
                  <DropdownMenuItem onSelect={() => actions.checkin(credential)}>
                    <CalendarCheck className="size-4" /> 签到
                  </DropdownMenuItem>
                  {credential.provider === "codebuddy" && (
                    <DropdownMenuItem onSelect={() => actions.growth(credential)}>
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
  onClose,
}: {
  credentialId: string;
  events: CreditEvent[];
  onClose: () => void;
}) {
  return (
    <div
      className="mt-4 rounded-lg border border-border/60 bg-muted/30 p-3"
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
              <TableHead>观测时间</TableHead>
              <TableHead>区间起点</TableHead>
              <TableHead>变化</TableHead>
              <TableHead>说明</TableHead>
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
    </div>
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
