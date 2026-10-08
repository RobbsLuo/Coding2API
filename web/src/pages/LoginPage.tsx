import { useState } from "react";
import { ArrowRight, Boxes, ShieldCheck, TriangleAlert, Zap } from "lucide-react";
import { api, ApiError } from "../api/client";
import { BrandMark } from "../components/Sidebar";
import { Button, Input, Notice } from "../ui";

/** 左侧品牌区的三条卖点：说明这个网关到底解决什么。 */
const HIGHLIGHTS = [
  {
    icon: Boxes,
    title: "六渠道统一调度",
    text: "CodeBuddy / TRAE / Qoder / CodeArts 与免费层汇入单一 OpenAI 兼容出口。",
  },
  {
    icon: Zap,
    title: "健康度自动选号",
    text: "按剩余额度与冷却状态挑号，失败自动轮换，会话粘性保持一致。",
  },
  {
    icon: ShieldCheck,
    title: "凭证加密入库",
    text: "账号集中维护、全员共享；用量按 Key 归属统计并留审计。",
  },
];

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
    <div className="grid min-h-full lg:grid-cols-[1.1fr_1fr]">
      {/* 品牌区（桌面显示）：产品故事 + 卖点 */}
      <aside className="relative hidden flex-col justify-between overflow-hidden bg-[var(--surface-sunken)] p-10 lg:flex">
        {/* 细微网格纹理：控制台质感，不喧宾夺主 */}
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 opacity-[0.5]"
          style={{
            backgroundImage:
              "radial-gradient(color-mix(in oklch, var(--primary) 22%, transparent) 1px, transparent 1px)",
            backgroundSize: "22px 22px",
          }}
        />
        <div className="relative flex items-center gap-2.5">
          <BrandMark className="size-9" />
          <span className="text-lg font-semibold tracking-tight">Coding2API</span>
        </div>
        <div className="relative max-w-md space-y-6">
          <p className="text-2xl leading-snug font-semibold tracking-tight">
            多路渠道，一个出口。
          </p>
          <ul className="space-y-5">
            {HIGHLIGHTS.map((item) => (
              <li key={item.title} className="flex gap-3">
                <span className="mt-0.5 grid size-8 shrink-0 place-items-center rounded-lg bg-primary/10 text-primary">
                  <item.icon className="size-4" />
                </span>
                <div>
                  <div className="text-sm font-medium">{item.title}</div>
                  <p className="text-xs leading-relaxed text-muted-foreground">{item.text}</p>
                </div>
              </li>
            ))}
          </ul>
        </div>
        <p className="relative text-xs text-muted-foreground">
          仅供学习研究，未做安全审计。
        </p>
      </aside>

      {/* 表单区 */}
      <div className="grid place-items-center px-6 py-12">
        <form onSubmit={submit} className="w-full max-w-sm">
          <div className="mb-8 text-center lg:text-left">
            <BrandMark className="mx-auto mb-3 size-11 rounded-xl lg:hidden" />
            <h1 className="text-2xl font-semibold tracking-tight">登录管理台</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              多渠道统一调度 · OpenAI 兼容
            </p>
          </div>

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
              <div data-testid="login-error" role="alert">
                <Notice tone="danger">
                  <span className="inline-flex items-center gap-1.5">
                    <TriangleAlert className="size-3.5" />
                    {error}
                  </span>
                </Notice>
              </div>
            )}
            <Button
              type="submit"
              variant="primary"
              disabled={pending}
              className="w-full gap-1.5"
            >
              {pending ? "登录中…" : "登录"}
              {!pending && <ArrowRight className="size-4" />}
            </Button>
          </div>

          <p className="mt-6 text-center text-xs leading-relaxed text-muted-foreground lg:text-left">
            账号由管理员在「用户管理」中创建，你会收到一次性激活链接来自设密码。
          </p>
        </form>
      </div>
    </div>
  );
}
