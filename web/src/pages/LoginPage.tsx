import { useState } from "react";
import { api, ApiError } from "../api/client";
import { Button, Field, Input, Notice, Panel } from "../ui";

export function LoginPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      await api.login(username, password);
      // 会话由 Cookie 承载，刷新以让 App 的会话查询重新取值
      window.location.href = "/";
    } catch (caught) {
      const status = caught instanceof ApiError ? caught.status : 0;
      setError(status === 401 ? "用户名或密码错误" : "登录失败，请稍后重试");
      setPending(false);
    }
  };

  return (
    <div className="grid min-h-full place-items-center px-6">
      <form onSubmit={submit} className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <h1 className="text-lg font-semibold tracking-tight">coding2api</h1>
          <p className="mt-1 text-xs text-[var(--color-ink-muted)]">管理台登录</p>
        </div>
        <Panel>
          <div className="space-y-4">
            <Field label="用户名">
              <Input
                value={username}
                autoComplete="username"
                data-testid="login-username"
                onChange={(event) => setUsername(event.target.value)}
              />
            </Field>
            <Field label="密码">
              <Input
                type="password"
                value={password}
                autoComplete="current-password"
                data-testid="login-password"
                onChange={(event) => setPassword(event.target.value)}
              />
            </Field>
            {error && (
              <div data-testid="login-error">
                <Notice tone="danger">{error}</Notice>
              </div>
            )}
            <Button type="submit" variant="primary" disabled={pending} >
              {pending ? "登录中…" : "登录"}
            </Button>
          </div>
        </Panel>
        <p className="mt-4 text-center text-xs text-[var(--color-ink-muted)]">
          用户来自 <code>secrets/users.txt</code>，使用 <code>scripts/hash_password.py</code> 添加。
        </p>
      </form>
    </div>
  );
}
