import { NavLink, Outlet, useOutletContext } from "react-router-dom";
import {
  BarChart3,
  Database,
  Globe,
  KeyRound,
  LayoutDashboard,
  Menu,
  SlidersHorizontal,
  TerminalSquare,
} from "lucide-react";
import type { SessionInfo } from "./api/types";
import { Button } from "./components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "./components/ui/dropdown-menu";
import { ThemeToggle } from "./components/ThemeToggle";
import { UserMenu } from "./components/UserMenu";
import { cn } from "@/lib/utils";

interface NavItem {
  to: string;
  label: string;
  icon: typeof LayoutDashboard;
  end?: boolean;
  adminOnly?: boolean;
}

const NAV: NavItem[] = [
  { to: "/", label: "池仪表盘", icon: LayoutDashboard, end: true },
  { to: "/credentials", label: "凭证管理", icon: Database },
  { to: "/api-keys", label: "API Key", icon: KeyRound },
  { to: "/stats", label: "用量统计", icon: BarChart3 },
  { to: "/playground", label: "Playground", icon: TerminalSquare },
  // 仅管理员可见的写入口（页面自身也会被后端 403 挡住，这里只是不误导）
  { to: "/settings", label: "任务与配置", icon: SlidersHorizontal, adminOnly: true },
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
  // 非管理员看不到「任务与配置」：写接口是 admin-only，露出入口只会误导
  const nav = NAV.filter((item) => !item.adminOnly || session.is_admin);

  return (
    <div className="flex min-h-full flex-col">
      <header className="sticky top-0 z-20 border-b border-border bg-[color:color-mix(in_oklch,var(--background)_88%,transparent)] backdrop-blur">
        <div className="mx-auto flex w-full max-w-7xl items-center gap-2 px-3 py-2.5 sm:gap-4 sm:px-6">
          {/* 移动端导航：汉堡下拉，替代横向导航图标（sm 以下空间不足） */}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="sm:hidden"
                aria-label="打开导航菜单"
              >
                <Menu />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="w-52">
              {nav.map((item) => (
                <DropdownMenuItem key={item.to} asChild>
                  <NavLink to={item.to} end={item.end} className="cursor-pointer">
                    {({ isActive }) => (
                      <span
                        className={cn(
                          "flex items-center gap-2.5",
                          isActive && "font-medium text-primary",
                        )}
                      >
                        <item.icon className="size-4" />
                        {item.label}
                      </span>
                    )}
                  </NavLink>
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
          <span className="flex min-w-0 items-center gap-2 font-semibold tracking-tight">
            <BrandMark className="size-7" />
            Coding2API
          </span>
          <nav className="hidden flex-1 gap-1 sm:flex">
            {nav.map((item) => (
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
                    <span>{item.label}</span>
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