import type { ReactNode } from "react";
import { api } from "../../api/client";
import { formatNumber, probeFailureLabel } from "../../api/display";
import type { CheckinEvent, Credential, CreditEvent } from "../../api/types";
import type { ToastItem } from "../../components/Toast";
import type { Ledger } from "../../lib/credentialList";
import { errorText } from "../../lib/errors";
import { GrowthToastContent } from "./toasts";

/** 变动流水抽屉的打开态：一次只开一本账。 */
export interface CreditEventsState {
  credentialId: string;
  ledger: Ledger;
  events: CreditEvent[];
  sort: string;
  order: string;
}

/** 签到记录抽屉的打开态。 */
export interface CheckinLogState {
  credentialId: string;
  events: CheckinEvent[];
}

/**
 * 行内动作依赖的页面能力：全部由页面壳显式传入，不读模块级变量。
 * 拆出这一层只是搬家——动作的实现与原先逐字一致。
 */
export interface ActionsDeps {
  run: (task: () => Promise<unknown>, successMessage?: string) => Promise<void>;
  refresh: () => Promise<unknown>;
  push: (toast: Omit<ToastItem, "id">) => unknown;
  showError: (content: ReactNode) => void;
  showNotice: (content: ReactNode) => void;
  setBusy: (busy: boolean) => void;
  setConfirmDelete: (id: string | null) => void;
  creditEvents: CreditEventsState | null;
  setCreditEvents: (next: CreditEventsState | null) => void;
  checkinLog: CheckinLogState | null;
  setCheckinLog: (next: CheckinLogState | null) => void;
  setLimitsFor: (id: string) => void;
  setTagsFor: (id: string) => void;
  setWeightFor: (id: string) => void;
  setWebCookieFor: (id: string) => void;
  setLoginSecretFor: (id: string) => void;
}

export interface Actions {
  revive: (credential: Credential) => void;
  forceRevive: (credential: Credential) => void;
  toggle: (credential: Credential) => void;
  pin: (credential: Credential) => void;
  remove: (credential: Credential) => void;
  probe: (credential: Credential) => void;
  checkin: (credential: Credential) => void;
  growth: (credential: Credential) => void;
  activity: (credential: Credential) => void;
  credits: (credential: Credential, ledger: Ledger) => void;
  checkinLog: (credential: Credential) => void;
  limits: (credential: Credential) => void;
  tags: (credential: Credential) => void;
  weight: (credential: Credential) => void;
  webCookie: (credential: Credential) => void;
  loginSecret: (credential: Credential) => void;
  export: (credential: Credential) => void;
}
export function buildActions(deps: ActionsDeps): Actions {
  const {
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
  } = deps;
  return {
    // 会话失效被硬禁用的凭证：重新登录后用「恢复」放回池子，
    // 不必删除重建（以前只能删除）。**保留**上游点名的冷却（限流/余额/
    // 权益/风控）——清掉只会立刻再撞一次上游；要强制清走「强制恢复」。
    revive: (credential) =>
      void run(() => api.reviveCredential(credential.id), "已恢复该凭证，可参与调度。"),
    forceRevive: (credential) =>
      void run(
        () => api.reviveCredential(credential.id, true),
        "已强制恢复：上游点名的冷却（限流/余额/权益/风控）也一并清除，请留意可能立刻再撞上游。",
      ),
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
          showError(errorText(caught, "探测失败"));
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
          showError(errorText(caught, "签到失败"));
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
          showError(errorText(caught, "成长中心执行失败"));
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
          showError(errorText(caught, "流水读取失败"));
        }
      })(),
    // 签到记录：读本地落库的审计日志（不发上游请求，故对已失效凭证也能查）。
    // 与流水同形——再点一次即收起。
    checkinLog: (credential) =>
      void (async () => {
        if (checkinLog?.credentialId === credential.id) {
          setCheckinLog(null);
          return;
        }
        try {
          const result = await api.checkinEvents(credential.id);
          setCheckinLog({ credentialId: credential.id, events: result.events });
        } catch (caught) {
          showError(errorText(caught, "签到记录读取失败"));
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
          showError(errorText(caught, "活跃上报失败"));
        } finally {
          setBusy(false);
        }
      })(),
    // 只开对话框；保存与刷新由对话框自己回调（见 CredentialLimitsDialog）
    limits: (credential) => setLimitsFor(credential.id),
    tags: (credential) => setTagsFor(credential.id),
    weight: (credential) => setWeightFor(credential.id),
    // 单条导出：文件内容与「批量导入」的条目同形，可原样再导入。
    // 只开对话框；保存与探测由对话框/后端负责
    webCookie: (credential) => setWebCookieFor(credential.id),
    loginSecret: (credential) => setLoginSecretFor(credential.id),
    export: (credential) =>
      void (async () => {
        try {
          const { blob } = await api.exportCredential(credential.id);
          const url = URL.createObjectURL(blob);
          const anchor = document.createElement("a");
          anchor.href = url;
          anchor.download =
            `coding2api-credential-${credential.provider}-${credential.id}.json`;
          document.body.appendChild(anchor);
          anchor.click();
          anchor.remove();
          URL.revokeObjectURL(url);
          showNotice("已导出凭证 JSON（含明文令牌，请妥善保管）");
        } catch (caught) {
          showError(errorText(caught, "导出凭证失败"));
        }
      })(),
  };
}
