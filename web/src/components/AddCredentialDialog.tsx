import { useEffect, useRef, useState } from "react";
import { Plus, X } from "lucide-react";
import { api } from "../api/client";
import { PROVIDER_LABEL } from "../api/providers";
import type { Provider } from "../api/types";
import { Button, Field, Input, Notice, Select, Tabs, Textarea } from "../ui";

/** 支持「登录渠道账号」按钮 / JSON 导入下拉的渠道（有 poll 轨道或需人工导入的渠道）。 */
const LOGIN_PROVIDERS: Provider[] = ["codebuddy", "trae", "qoder", "codearts"];

type TabValue = "login" | "import";

/** 只允许 http(s) 授权地址：上游返回的 auth_url 若为 `javascript:` / `data:`
 * 等被赋给弹窗 location 会在弹窗（继承本页 origin）执行脚本（M5）。 */
function safeAuthUrl(raw: string | undefined): string | null {
  if (!raw) return null;
  try {
    const url = new URL(raw);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}

/**
 * 添加凭证对话框：登录渠道 / JSON 导入两分区 + 免费层一键补回。
 *
 * 取代原先常驻页面底部的「登录渠道账号」「导入凭证」两个面板——添加凭证
 * 是低频动作，不该常驻占屏。登录轮询 state 与 timer 全部收在这里：
 * 组件卸载时统一清掉（原先由页面负责）。
 *
 * TRAE（callback 轨道）授权完成靠凭证列表变化检测，对话框关闭轮询就停了，
 * 所以 TRAE 登录中时禁用关闭，等流程结束（成功/超时/取消）再放行。
 */
export function AddCredentialDialog({
  open,
  onClose,
  credentialCount,
  hasZen,
  hasKilo,
  onImported,
  onNotice,
  onError,
}: {
  open: boolean;
  onClose: () => void;
  /** 当前凭证数量：TRAE callback 轨道靠数量变化检测授权完成。 */
  credentialCount: number;
  hasZen: boolean;
  hasKilo: boolean;
  /** 登录/导入成功后回调（关对话框；列表刷新由调用方统一做）。 */
  onImported: (message: string) => void;
  /** 登录过程消息透出到页面横幅。 */
  onNotice: (message: string) => void;
  onError: (message: string) => void;
}) {
  const [tab, setTab] = useState<TabValue>("login");
  const [busy, setBusy] = useState(false);
  // 进行中的登录：state 用于 cancel 精确取消那一次流程，timer 用于卸载时清轮询
  const [loginProviders, setLoginProviders] = useState<Provider[]>([]);
  const loginStatesRef = useRef<Partial<Record<Provider, string>>>({});
  const loginTimersRef = useRef<Partial<Record<Provider, number>>>({});
  // CodeArts paste 轨道：授权后把浏览器地址栏里打不开的 127.0.0.1 回调链接粘回来
  const [pasteProvider, setPasteProvider] = useState<Provider | null>(null);
  const [pasteUrl, setPasteUrl] = useState("");
  // JSON 导入（原 ImportPanel 逻辑）
  const [importProvider, setImportProvider] = useState<Provider>("codebuddy");
  const [nickname, setNickname] = useState("");
  const [raw, setRaw] = useState("");
  const [importError, setImportError] = useState<string | null>(null);
  // TRAE 轮询要比较「登录发起时 vs 现在」的凭证数量；ref 让 interval 里
  // 读到的始终是最新值而不触发重渲染。
  const credentialCountRef = useRef(credentialCount);
  credentialCountRef.current = credentialCount;

  // 页面卸载/对话框卸载时清掉所有登录轮询，防止跨页面的僵尸 interval
  useEffect(() => () => {
    Object.values(loginTimersRef.current).forEach((timer) => {
      if (timer !== undefined) window.clearInterval(timer);
    });
  }, []);

  if (!open) return null;

  const stopPolling = (provider: Provider) => {
    window.clearInterval(loginTimersRef.current[provider]);
    delete loginTimersRef.current[provider];
    setLoginProviders((previous) => previous.filter((item) => item !== provider));
  };

  const startLogin = async (provider: Provider) => {
    onError("");
    onNotice("");
    // 先同步开一个占位窗口：window.open 若在 await 之后才调用，
    // 会脱离用户手势上下文而被浏览器弹窗拦截。
    const popup = window.open("", "_blank");
    if (popup) popup.opener = null;               // 切断对管理台 window 的引用（M5）
    try {
      const started = await api.upstreamStart(provider);
      loginStatesRef.current[provider] = started.state;
      if (!started.auth_url) {
        popup?.close();                        // 上游没给授权地址：关掉空白占位窗
      } else {
        const authUrl = safeAuthUrl(started.auth_url);
        if (!authUrl) {
          popup?.close();
          delete loginStatesRef.current[provider];
          onError("授权地址无效，已中止登录。");
          return;
        }
        if (popup && !popup.closed) popup.location.href = authUrl;
      }
      setLoginProviders((previous) => [...new Set([...previous, provider])]);

      if (started.flow === "callback") {
        // TRAE：浏览器完成授权后 302 回本服务的 /authorize，那里直接落库。
        // 前端无法轮询渠道，改为轮询凭证列表，出现新凭证即视为完成。
        onNotice("已在新标签页打开授权页。完成授权后本页会自动刷新出凭证。");
        const deadline = Date.now() + 5 * 60 * 1000;
        const baseline = credentialCountRef.current;
        const timer = window.setInterval(async () => {
          const after = (await api.credentials()).credentials.length;
          if (after > baseline || Date.now() > deadline) {
            stopPolling(provider);
            onNotice(after > baseline ? "登录成功，凭证已保存。" : "授权超时，请重新发起登录。");
          }
        }, 3000);
        loginTimersRef.current[provider] = timer;
        return;
      }

      if (started.flow === "paste") {
        // CodeArts：门户把 code 302 回 127.0.0.1 回调端口，服务端不监听，
        // 改为让用户把浏览器地址栏里的整条回调链接粘回来，服务端换 token。
        stopPolling(provider);
        setPasteProvider(provider);
        setPasteUrl("");
        onNotice(
          "已在新标签页打开华为云授权页。授权后会跳到一个打不开的本地地址（127.0.0.1），"
          + "把地址栏里的整条链接复制粘贴到下方即可完成登录。",
        );
        return;
      }

      onNotice("已在新标签页打开授权页，完成后此页会自动检测。");
      const interval = (started.interval ?? 5) * 1000;
      const timer = window.setInterval(async () => {
        try {
          const result = await api.upstreamPoll(provider, started.state);
          if (result.status === "success") {
            stopPolling(provider);
            onNotice("登录成功，凭证已保存。");
            onImported("登录成功，凭证已保存。");
          }
        } catch {
          stopPolling(provider);
          onError("登录轮询失败，请重试。");
        }
      }, interval);
      loginTimersRef.current[provider] = timer;
    } catch (caught) {
      // 启动失败：关掉占位窗口，并把授权地址给出来让用户手动打开
      popup?.close();
      onError(caught instanceof Error ? caught.message : "无法启动登录流程");
    }
  };

  const cancelLogin = async (provider: Provider) => {
    onError("");
    // 用 startLogin 记下的 state 取消当前那次流程；并停掉对应的凭证列表轮询。
    const state = loginStatesRef.current[provider];
    const timer = loginTimersRef.current[provider];
    if (timer !== undefined) {
      window.clearInterval(timer);
      delete loginTimersRef.current[provider];
    }
    delete loginStatesRef.current[provider];
    if (pasteProvider === provider) {
      setPasteProvider(null);
      setPasteUrl("");
    }
    try {
      if (state) await api.upstreamCancel(provider, state).catch(() => undefined);
    } finally {
      setLoginProviders((previous) => previous.filter((item) => item !== provider));
      onNotice("已取消登录。");
    }
  };

  const completeLogin = async () => {
    if (!pasteProvider) return;
    const state = loginStatesRef.current[pasteProvider];
    if (!state) {
      onError("登录会话已失效，请重新发起登录。");
      return;
    }
    setBusy(true);
    onError("");
    try {
      await api.upstreamComplete(pasteProvider, state, pasteUrl.trim());
      delete loginStatesRef.current[pasteProvider];
      stopPolling(pasteProvider);
      setPasteProvider(null);
      setPasteUrl("");
      onNotice("登录成功，凭证已保存。");
      onImported("登录成功，凭证已保存。");
    } catch (caught) {
      onError(caught instanceof Error ? caught.message : "登录失败，请检查粘贴的链接。");
    } finally {
      setBusy(false);
    }
  };

  const submitImport = async (event: React.FormEvent) => {
    event.preventDefault();
    setImportError(null);
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      setImportError('凭证必须是合法 JSON。CodeBuddy 可填 {"token":"..."}，TRAE 填完整凭证对象。');
      return;
    }
    setBusy(true);
    try {
      await api.importCredential(importProvider, parsed, nickname);
      setRaw("");
      setNickname("");
      onImported("凭证已导入。");
    } catch (caught) {
      onError(caught instanceof Error ? caught.message : "导入失败");
    } finally {
      setBusy(false);
    }
  };

  const requestClose = () => {
    // TRAE 登录中不能关：授权完成的检测靠这里的凭证列表轮询，关了就停。
    if (loginProviders.includes("trae")) return;
    onClose();
  };

  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center bg-black/50 p-4"
      onClick={requestClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="添加凭证"
        data-testid="add-credential-dialog"
        onClick={(event) => event.stopPropagation()}
        className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-xl border border-border bg-background p-5 shadow-lg"
      >
        <div className="mb-4 flex items-center justify-between">
          <h2 className="flex items-center gap-2 text-sm font-semibold">
            <Plus className="size-4 text-muted-foreground" />
            添加凭证
          </h2>
          <Button
            size="icon"
            variant="ghost"
            aria-label="关闭"
            data-testid="add-credential-close"
            disabled={loginProviders.includes("trae")}
            onClick={requestClose}
          >
            <X className="size-4" />
          </Button>
        </div>

        <Tabs
          value={tab}
          testId="add-credential-tab"
          onChange={(value) => setTab(value as TabValue)}
          options={[
            { value: "login", label: "登录渠道" },
            { value: "import", label: "JSON 导入" },
          ]}
        />

        {tab === "login" ? (
          <div className="mt-4 space-y-4" data-testid="add-credential-login">
            <div className="flex flex-wrap items-center gap-3">
              {LOGIN_PROVIDERS.map((item) => {
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
            <p className="text-xs text-muted-foreground">
              CodeBuddy 走设备码轮询（自动检测完成）；TRAE 走浏览器回调
              （授权后由 <code>/authorize</code> 直接落库，本对话框轮询凭证列表检测完成，
              期间请勿关闭对话框）；Qoder 走设备码登录；CodeArts 走 OAuth2 授权码登录
              （授权后把浏览器地址栏里打不开的 127.0.0.1 回调链接粘回本对话框）。
            </p>
            {pasteProvider && (
              <div className="space-y-2" data-testid="paste-callback">
                <Field label={`${PROVIDER_LABEL[pasteProvider]} 授权回调链接`}>
                  <Input
                    data-testid="paste-callback-input"
                    value={pasteUrl}
                    onChange={(event) => setPasteUrl(event.target.value)}
                    placeholder="http://127.0.0.1:12800/oauth/callback?code=…&state=…"
                  />
                </Field>
                <div className="flex items-center gap-2">
                  <Button
                    size="sm"
                    variant="primary"
                    data-testid="paste-callback-submit"
                    disabled={busy || pasteUrl.trim().length === 0}
                    onClick={() => void completeLogin()}
                  >
                    完成登录
                  </Button>
                  <Button
                    size="sm"
                    variant="danger"
                    data-testid="paste-callback-cancel"
                    onClick={() => void cancelLogin(pasteProvider)}
                  >
                    取消
                  </Button>
                </div>
              </div>
            )}
            <div className="border-t border-border pt-3">
              <div className="mb-2 text-xs text-muted-foreground">
                OpenCode Zen / Kilo Gateway 免费层无需凭证/登录，删除其虚拟凭证后点下方按钮补回
                （要永久停用请改用「暂停」）。
              </div>
              <div className="flex flex-wrap gap-2">
                <Button
                  size="sm"
                  variant="default"
                  data-testid="add-zen"
                  disabled={busy || hasZen}
                  onClick={async () => {
                    setBusy(true);
                    try {
                      await api.importCredential("zen", {}, "OpenCode Zen");
                      onImported("已添加 OpenCode Zen 免费渠道。");
                    } catch (caught) {
                      onError(caught instanceof Error ? caught.message : "添加失败");
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  {hasZen ? "OpenCode Zen 已添加" : "添加 OpenCode Zen"}
                </Button>
                <Button
                  size="sm"
                  variant="default"
                  data-testid="add-kilo"
                  disabled={busy || hasKilo}
                  onClick={async () => {
                    setBusy(true);
                    try {
                      await api.importCredential("kilo", {}, "Kilo Gateway");
                      onImported("已添加 Kilo Gateway 免费渠道。");
                    } catch (caught) {
                      onError(caught instanceof Error ? caught.message : "添加失败");
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  {hasKilo ? "Kilo Gateway 已添加" : "添加 Kilo Gateway"}
                </Button>
              </div>
            </div>
          </div>
        ) : (
          <form onSubmit={submitImport} className="mt-4 space-y-3" data-testid="add-credential-import">
            <div className="flex flex-wrap gap-3">
              <div className="w-40">
                <Field label="渠道">
                  <Select
                    value={importProvider}
                    data-testid="import-provider"
                    onChange={(event) => setImportProvider(event.target.value as Provider)}
                  >
                    {LOGIN_PROVIDERS.map((item) => (
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
            {importError && (
              <div data-testid="import-error">
                <Notice tone="danger">{importError}</Notice>
              </div>
            )}
            <Button type="submit" variant="primary" disabled={busy} data-testid="import-submit">
              导入
            </Button>
          </form>
        )}
      </div>
    </div>
  );
}
