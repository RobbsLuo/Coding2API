import { useState } from "react";
import { TerminalSquare, TriangleAlert } from "lucide-react";
import { api, ApiError } from "../api/client";
import { Button, Input, Notice, Panel } from "../ui";

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
    <div className="grid min-h-full place-items-center bg-[color:color-mix(in_oklch,var(--accent)_4%,var(--background))] px-6">
      <form onSubmit={submit} className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <div className="mx-auto mb-3 grid size-11 place-items-center rounded-xl bg-primary text-primary-foreground shadow-lg shadow-primary/25">
            <TerminalSquare className="size-6" />
          </div>
          <h1 className="text-xl font-semibold tracking-tight">Coding2API</h1>
          <p className="mt-1 text-xs text-muted-foreground">双上游统一调度 · OpenAI 兼容管理台</p>
        </div>
        <Panel className="shadow-lg shadow-foreground/5">
          <div className="space-y-4">
            <label className="block space-y-1.5">
              <span className="text-xs font-medium text-muted-foreground">用户名</span>
              <Input
                value={username}
                autoComplete="username"
                placeholder="admin"
                data-testid="login-username"
                onChange={(event) => setUsername(event.target.value)}
              />
            </label>
            <label className="block space-y-1.5">
              <span className="text-xs font-medium text-muted-foreground">密码</span>
              <Input
                type="password"
                value={password}
                autoComplete="current-password"
                placeholder="••••••••"
                data-testid="login-password"
                onChange={(event) => setPassword(event.target.value)}
              />
            </label>
            {error && (
              <div data-testid="login-error">
                <Notice tone="danger">
                  <span className="inline-flex items-center gap-1.5">
                    <TriangleAlert className="size-3.5" />
                    {error}
                  </span>
                </Notice>
              </div>
            )}
            <Button type="submit" variant="primary" disabled={pending} className="w-full">
              {pending ? "登录中…" : "登录"}
            </Button>
          </div>
        </Panel>
        <p className="mt-4 text-center text-xs text-muted-foreground">
          用户来自 <code>secrets/users.txt</code>，使用 <code>scripts/hash_password.py</code> 添加。
        </p>
      </form>
    </div>
  );
}