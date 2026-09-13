import { NavLink, Outlet, useOutletContext } from "react-router-dom";
import {
  BarChart3,
  Database,
  Globe,
  KeyRound,
  LayoutDashboard,
  TerminalSquare,
} from "lucide-react";
import type { SessionInfo } from "./api/types";
import { ThemeToggle } from "./components/ThemeToggle";
import { UserMenu } from "./components/UserMenu";
import { cn } from "@/lib/utils";

const NAV = [
  { to: "/", label: "池仪表盘", icon: LayoutDashboard, end: true },
  { to: "/credentials", label: "凭证管理", icon: Database },
  { to: "/api-keys", label: "API Key", icon: KeyRound },
  { to: "/stats", label: "用量统计", icon: BarChart3 },
  { to: "/playground", label: "Playground", icon: TerminalSquare },
];

/** 品牌标记：多路渠道管道汇聚进单一出口（Coding2API 的产品故事）。 */
export function BrandMark({ className }: { className?: string }) {
  return (
    <span
      className={cn(
        "grid place-items-center rounded-lg bg-primary text-primary-foreground",
        className,
      )}
      aria-hidden
    >
      <svg viewBox="0 0 16 16" className="size-[60%]" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
        <path d="M2.5 3.5 6.5 8 2.5 12.5" />
        <path d="M13.5 3.5 9.5 8l4 4.5" />
        <path d="M6.5 8h4" />
        <circle cx="8" cy="8" r="1.4" fill="currentColor" stroke="none" />
      </svg>
    </span>
  );
}

export function Layout({ session }: { session: SessionInfo }) {
  const logout = async () => {
    await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
    window.location.href = "/login";
  };

  return (
    <div className="flex min-h-full flex-col">
      <header className="sticky top-0 z-20 border-b border-border bg-[color:color-mix(in_oklch,var(--background)_88%,transparent)] backdrop-blur">
        <div className="mx-auto flex w-full max-w-7xl items-center gap-3 px-4 py-2.5 sm:gap-4 sm:px-6">
          <span className="flex items-center gap-2 font-semibold tracking-tight">
            <BrandMark className="size-7" />
            Coding2API
          </span>
          <nav className="flex flex-1 gap-1">
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  cn(
                    "flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-sm transition-colors sm:px-3",
                    isActive
                      ? "bg-primary/10 font-medium text-primary"
                      : "text-muted-foreground hover:bg-muted hover:text-foreground",
                  )
                }
              >
                {() => (
                  <>
                    <item.icon className="size-4 shrink-0" />
                    <span className="hidden sm:inline">{item.label}</span>
                  </>
                )}
              </NavLink>
            ))}
          </nav>
          <a
            href="https://github.com/RobbsLuo/coding2api"
            target="_blank"
            rel="noopener noreferrer"
            aria-label="项目仓库"
            title="项目仓库"
            className="grid size-8 place-items-center rounded-lg text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          >
            <Globe className="size-4" />
          </a>
          <ThemeToggle />
          <UserMenu
            username={session.username}
            isAdmin={session.is_admin}
            onLogout={logout}
          />
        </div>
      </header>
      <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-6 sm:px-6">
        <Outlet context={session} />
      </main>
      <footer className="mx-auto w-full max-w-7xl px-4 py-4 text-xs text-muted-foreground sm:px-6">
        Coding2API · 仅供学习研究，未做安全审计
      </footer>
    </div>
  );
}

/** 页面通过 useOutletContext 取当前会话（含 is_admin，决定写操作可见性）。 */
export function useSessionContext(): SessionInfo {
  return useOutletContext<SessionInfo>();
}