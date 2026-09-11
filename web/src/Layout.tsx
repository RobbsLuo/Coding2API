import { NavLink, Outlet, useOutletContext } from "react-router-dom";
import type { SessionInfo } from "./api/types";
import { Button } from "./ui";

const NAV = [
  { to: "/", label: "池仪表盘" },
  { to: "/credentials", label: "凭证管理" },
  { to: "/api-keys", label: "API Key" },
  { to: "/stats", label: "用量统计" },
  { to: "/playground", label: "Playground" },
];

export function Layout({ session }: { session: SessionInfo }) {
  const logout = async () => {
    await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
    window.location.href = "/login";
  };

  return (
    <div className="flex min-h-full flex-col">
      <header className="border-b border-[var(--color-border-soft)] bg-[var(--color-panel)]">
        <div className="mx-auto flex w-full max-w-6xl items-center gap-6 px-6 py-3">
          <span className="font-semibold tracking-tight">coding2api</span>
          <nav className="flex flex-1 gap-1">
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === "/"}
                className={({ isActive }) =>
                  `rounded-lg px-3 py-1.5 text-sm ${
                    isActive
                      ? "bg-[var(--color-accent)]/12 font-medium text-[var(--color-accent)]"
                      : "text-[var(--color-ink-muted)] hover:bg-[var(--color-surface)]"
                  }`
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
          <span className="text-xs text-[var(--color-ink-muted)]">
            {session.username}
            {session.is_admin ? " · 管理员" : " · 只读"}
          </span>
          <Button size="sm" variant="ghost" onClick={logout}>
            退出
          </Button>
        </div>
      </header>
      <main className="mx-auto w-full max-w-6xl flex-1 px-6 py-6">
        <Outlet context={session} />
      </main>
    </div>
  );
}

/** 页面通过 useOutletContext 取当前会话（含 is_admin，决定写操作可见性）。 */
export function useSessionContext(): SessionInfo {
  return useOutletContext<SessionInfo>();
}
