import { useState } from "react";
import { api } from "../api/client";
import { useSessionContext } from "../Layout";
import { useCredentials, useQueryClient } from "../api/hooks";
import {
  credentialState,
  cooldownRemaining,
  formatDuration,
  formatNumber,
  formatTime,
  healthView,
  probeFailureLabel,
  quotaSemantics,
  STATE_LABEL,
  STATE_TONE,
} from "../api/display";
import type { Credential, Provider } from "../api/types";
import { Badge, Button, Empty, Field, Input, Notice, Panel, Select, Textarea } from "../ui";

const PROVIDERS: Provider[] = ["codebuddy", "trae"];
const PROVIDER_LABEL: Record<Provider, string> = { codebuddy: "CodeBuddy", trae: "TRAE" };

interface Actions {
  toggle: (credential: Credential) => void;
  pin: (credential: Credential) => void;
  remove: (credential: Credential) => void;
  probe: (credential: Credential) => void;
  checkin: (credential: Credential) => void;
  openAccounts: (credential: Credential) => void;
}

export function CredentialsPage() {
  const session = useSessionContext();
  const { data, isLoading } = useCredentials(session.username);
  const client = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [accountsFor, setAccountsFor] = useState<string | null>(null);
  const [accounts, setAccounts] = useState<{ account_id: string; nickname: string; type: string }[]>([]);
  // 每个上游各自可能有进行中的登录（CodeBuddy 轮询 / TRAE 回调）
  const [loginProviders, setLoginProviders] = useState<Provider[]>([]);
  const [probeDetail, setProbeDetail] = useState<string | null>(null);

  const credentials = data?.credentials ?? [];
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
          setNotice(
            result.ok
              ? `签到成功，获得 ${formatNumber(result.credit)} 积分`
              : `签到未成功（code=${result.code ?? "null"}）${result.message ? ` ${result.message}` : ""}`,
          );
          await refresh();
        } catch (caught) {
          setError(caught instanceof Error ? caught.message : "签到失败");
        } finally {
          setBusy(false);
        }
      })(),
    openAccounts: (credential) =>
      void (async () => {
        setAccountsFor(credential.id);
        setError(null);
        try {
          setAccounts((await api.accounts(credential.id)).accounts);
        } catch (caught) {
          setAccounts([]);
          setError(caught instanceof Error ? caught.message : "账号列表获取失败");
        }
      })(),
  };

  const startLogin = async (provider: Provider) => {
    setError(null);
    setNotice(null);
    try {
      const started = await api.upstreamStart(provider);
      if (started.auth_url) window.open(started.auth_url, "_blank", "noopener");
      setLoginProviders((previous) => [...new Set([...previous, provider])]);

      if (started.flow === "callback") {
        // TRAE：浏览器完成授权后 302 回本服务的 /authorize，那里直接落库。
        // 前端无法轮询上游，改为轮询凭证列表，出现新凭证即视为完成。
        setNotice("已在新标签页打开授权页。完成授权后本页会自动刷新出凭证。");
        const deadline = Date.now() + 5 * 60 * 1000;
        const timer = window.setInterval(async () => {
          const before = credentials.length;
          await refresh();
          const after = (await api.credentials()).credentials.length;
          if (after > before || Date.now() > deadline) {
            window.clearInterval(timer);
            setLoginProviders((previous) => previous.filter((item) => item !== provider));
            setNotice(after > before ? "登录成功，凭证已保存。" : "授权超时，请重新发起登录。");
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
      {!isAdmin && (
        <div data-testid="readonly-banner">
          <Notice tone="muted">只读模式：仅管理员可以导入、启停或删除凭证。</Notice>
        </div>
      )}
      {credentials.length === 0 && isAdmin && (
        <div data-testid="first-run-hint">
          <Notice tone="muted">
            还没有凭证。点右上角「功能说明」查看各按钮的含义；或用下方「登录上游账号」
            完成 CodeBuddy / TRAE 授权，也可以直接粘贴凭证 JSON 导入。
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
              <span className="ml-2 text-[var(--color-ink-muted)]" data-testid="probe-detail">
                原始错误：{probeDetail}
              </span>
            )}
          </Notice>
        </div>
      )}

      <Panel title="凭证池">
        {credentials.length === 0 ? (
          <Empty data-testid="no-credentials">还没有凭证</Empty>
        ) : (
          <table className="w-full text-sm" data-testid="credentials-table">
            <thead>
              <tr className="text-left text-xs text-[var(--color-ink-muted)]">
                <th className="pb-2 font-medium">昵称</th>
                <th className="pb-2 font-medium">上游</th>
                <th className="pb-2 font-medium">状态</th>
                <th className="pb-2 font-medium">健康度</th>
                <th className="pb-2 font-medium">额度</th>
                {isAdmin && <th className="pb-2 font-medium">操作</th>}
              </tr>
            </thead>
            <tbody>
              {credentials.map((credential) => (
                <Row
                  key={credential.id}
                  credential={credential}
                  now={now}
                  isAdmin={isAdmin}
                  busy={busy}
                  confirming={confirmDelete === credential.id}
                  onConfirmDelete={setConfirmDelete}
                  actions={actions}
                />
              ))}
            </tbody>
          </table>
        )}
      </Panel>

      {accountsFor && isAdmin && (
        <Panel
          title="切换账号"
          action={
            <Button size="sm" variant="ghost" onClick={() => setAccountsFor(null)}>
              关闭
            </Button>
          }
        >
          {accounts.length === 0 ? (
            <Empty>没有可用账号</Empty>
          ) : (
            <ul className="space-y-1.5" data-testid="accounts-list">
              {accounts.map((account) => (
                <li key={account.account_id} className="flex items-center justify-between text-sm">
                  <span>
                    {account.nickname || account.account_id}
                    <span className="ml-2 text-xs text-[var(--color-ink-muted)]">
                      {account.type || "未知类型"}
                    </span>
                  </span>
                  <Button
                    size="sm"
                    disabled={busy}
                    onClick={() =>
                      void run(
                        () => api.selectAccount(accountsFor, account.account_id),
                        "账号已切换。",
                      ).then(() => setAccountsFor(null))
                    }
                  >
                    切换到此账号
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      )}

      {isAdmin && (
        <ImportPanel
          busy={busy}
          onImport={async (provider, credential, nickname) => {
            await run(() => api.importCredential(provider, credential, nickname), "凭证已导入。");
          }}
        />
      )}

      {isAdmin && (
        <Panel title="登录上游账号">
          <div className="flex flex-wrap items-center gap-3">
            {PROVIDERS.map((item) => {
              const pending = loginProviders.includes(item);
              return pending ? (
                <span key={item} className="inline-flex items-center gap-2">
                  <span className="text-xs text-[var(--color-ink-muted)]">
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
          <p className="mt-2 text-xs text-[var(--color-ink-muted)]">
            CodeBuddy 走设备码轮询（本页自动轮询上游）；TRAE 走浏览器回调
            （授权后由 <code>/authorize</code> 直接落库，本页轮询凭证列表检测完成）。
            也可以直接粘贴凭证 JSON 导入。
          </p>
        </Panel>
      )}
    </div>
  );
}

function Row({
  credential,
  now,
  isAdmin,
  busy,
  confirming,
  onConfirmDelete,
  actions,
}: {
  credential: Credential;
  now: number;
  isAdmin: boolean;
  busy: boolean;
  confirming: boolean;
  onConfirmDelete: (id: string | null) => void;
  actions: Actions;
}) {
  const health = healthView(credential.health);
  const state = credentialState(credential, now);
  const cooldown = cooldownRemaining(credential.cooling_until, now);

  return (
    <tr className="border-t border-[var(--color-border-soft)]" data-testid={`row-${credential.id}`}>
      <td className="py-2">
        {credential.nickname || credential.id.slice(0, 12)}
        {credential.pinned === 1 && (
          <span className="ml-2">
            <Badge tone="accent">已指定</Badge>
          </span>
        )}
      </td>
      <td className="py-2 text-xs">{PROVIDER_LABEL[credential.provider]}</td>
      <td className="py-2">
        <Badge tone={STATE_TONE[state]}>{STATE_LABEL[state]}</Badge>
        {state === "cooling" && (
          <span className="ml-2 text-xs text-[var(--color-ink-muted)]">{formatDuration(cooldown)}</span>
        )}
        {credential.disabled_reason && state === "disabled" && (
          <span className="ml-2 text-xs text-[var(--color-ink-muted)]">
            {credential.disabled_reason}
          </span>
        )}
      </td>
      <td className="py-2">
        <Badge tone={health.tone}>{health.label}</Badge>
        {health.kind === "unknown" && (
          <span className="ml-2 text-xs text-[var(--color-ink-muted)]">探测失败或未提供</span>
        )}
      </td>
      <td className="py-2 text-xs">
        {formatNumber(credential.quota_remaining)} / {formatNumber(credential.quota_total)}
        <div className="text-[var(--color-ink-muted)]">{quotaSemantics(credential)}</div>
        <div className="text-[var(--color-ink-muted)]">
          探测于 {formatTime(credential.quota_probed_at)}
        </div>
      </td>
      {isAdmin && (
        <td className="py-2">
          <div className="flex flex-wrap justify-end gap-1.5">
            <Button size="sm" disabled={busy} onClick={() => actions.probe(credential)}>
              探测
            </Button>
            <Button size="sm" disabled={busy} onClick={() => actions.checkin(credential)}>
              签到
            </Button>
            <Button size="sm" disabled={busy} onClick={() => actions.openAccounts(credential)}>
              账号
            </Button>
            <Button size="sm" disabled={busy} onClick={() => actions.toggle(credential)}>
              {credential.enabled === 1 ? "停用" : "启用"}
            </Button>
            <Button size="sm" disabled={busy} onClick={() => actions.pin(credential)}>
              {credential.pinned === 1 ? "取消指定" : "指定"}
            </Button>
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
              <Button
                size="sm"
                variant="ghost"
                onClick={() => onConfirmDelete(credential.id)}
              >
                删除
              </Button>
            )}
          </div>
        </td>
      )}
    </tr>
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
            <Field label="上游">
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
