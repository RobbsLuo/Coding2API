import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { ShieldCheck, TriangleAlert } from "lucide-react";
import { api, ApiError } from "../api/client";
import { BrandMark } from "../Layout";
import { Button, Field, Input, Notice, Panel } from "../ui";

/** 与后端 src/api/admin_auth.py 的 MIN_PASSWORD_LENGTH 一致。 */
const MIN_PASSWORD_LENGTH = 8;

/**
 * 账号激活页：用户凭一次性令牌自设密码。
 *
 * 无需登录（令牌即凭证）。令牌一次性且有时效；成功后跳登录页。
 * URL 上带 token 是必须的——这是用户唯一持有的凭证。
 */
export function ActivatePage() {
  const [params] = useSearchParams();
  const token = params.get("token") ?? "";

  const [username, setUsername] = useState<string | null>(null);
  const [invalid, setInvalid] = useState(false);
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [done, setDone] = useState(false);

  useEffect(() => {
    let cancelled = false;
    if (!token) {
      setInvalid(true);
      return;
    }
    api
      .activation(token)
      .then((result) => {
        if (!cancelled) setUsername(result.username);
      })
      .catch(() => {
        if (!cancelled) setInvalid(true);
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    if (password.length < MIN_PASSWORD_LENGTH) {
      setError(`密码至少 ${MIN_PASSWORD_LENGTH} 位`);
      return;
    }
    if (password !== confirm) {
      setError("两次输入的密码不一致");
      return;
    }
    setPending(true);
    try {
      await api.activate(token, password);
      setDone(true);
    } catch (caught) {
      const message = caught instanceof ApiError ? caught.message : "";
      setError(message.includes("activation") ? "链接无效或已过期，请向管理员索取新链接。" : "设置失败，请稍后重试。");
      setPending(false);
    }
  };

  return (
    <div className="grid min-h-full place-items-center bg-[color:color-mix(in_oklch,var(--accent)_4%,var(--background))] px-6">
      <div className="w-full max-w-sm" data-testid="activate-page">
        <div className="mb-6 text-center">
          <BrandMark className="mx-auto mb-3 size-12 rounded-xl" />
          <h1 className="text-xl font-semibold tracking-tight">激活账号</h1>
          <p className="mt-1 text-xs text-muted-foreground">设置你的密码以启用账号</p>
        </div>
        <Panel>
          {invalid ? (
            <div data-testid="activate-invalid">
              <Notice tone="danger">
                <span className="inline-flex items-center gap-1.5">
                  <TriangleAlert className="size-3.5" />
                  链接无效或已过期，请向管理员索取新的激活链接。
                </span>
              </Notice>
            </div>
          ) : done ? (
            <div className="space-y-4 text-center" data-testid="activate-done">
              <Notice tone="ok">
                <span className="inline-flex items-center gap-1.5">
                  <ShieldCheck className="size-3.5" />
                  密码已设置，现在可以登录了。
                </span>
              </Notice>
              <Button variant="primary" className="w-full" onClick={() => (window.location.href = "/login")}>
                前往登录
              </Button>
            </div>
          ) : (
            <form onSubmit={submit} className="space-y-4">
              <div className="text-sm text-muted-foreground">
                正在为 <span className="font-medium text-foreground">{username ?? "…"}</span> 设置密码
              </div>
              <Field label={`新密码（至少 ${MIN_PASSWORD_LENGTH} 位）`}>
                <Input
                  type="password"
                  value={password}
                  autoComplete="new-password"
                  data-testid="activate-password"
                  disabled={pending || username === null}
                  onChange={(event) => setPassword(event.target.value)}
                />
              </Field>
              <Field label="确认密码">
                <Input
                  type="password"
                  value={confirm}
                  autoComplete="new-password"
                  data-testid="activate-confirm"
                  disabled={pending || username === null}
                  onChange={(event) => setConfirm(event.target.value)}
                />
              </Field>
              {error && (
                <div data-testid="activate-error">
                  <Notice tone="danger">{error}</Notice>
                </div>
              )}
              <Button
                type="submit"
                variant="primary"
                className="w-full"
                disabled={pending || username === null}
              >
                {pending ? "提交中…" : "设置密码"}
              </Button>
            </form>
          )}
        </Panel>
      </div>
    </div>
  );
}
