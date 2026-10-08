import { useEffect, useRef, useState } from "react";
import { Outlet, useLocation, useOutletContext } from "react-router-dom";
import { GitBranch, Menu, X } from "lucide-react";
import type { SessionInfo } from "./api/types";
import { Button } from "./components/ui/button";
import { ChangePasswordDialog } from "./components/ChangePasswordDialog";
import { ThemeToggle } from "./components/ThemeToggle";
import { UserMenu } from "./components/UserMenu";
import { BrandMark, SidebarNav } from "./components/Sidebar";
import { useDialogFocus } from "./hooks/useDialogFocus";

// 兼容既有引用（登录页/激活页从 Layout 取品牌标记）
export { BrandMark } from "./components/Sidebar";

/** 品牌区：标记 + 字标。 */
function Brand() {
  return (
    <span className="flex min-w-0 items-center gap-2 font-semibold tracking-tight">
      <BrandMark className="size-7" />
      <span className="truncate">Coding2API</span>
    </span>
  );
}

export function Layout({ session }: { session: SessionInfo }) {
  const [changingPassword, setChangingPassword] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const location = useLocation();
  const mainRef = useRef<HTMLElement>(null);
  // 抽屉的 Esc 关闭 / 焦点陷阱 / 滚动锁由 hook 统一处理。
  const drawerRef = useDialogFocus<HTMLDivElement>(() => setDrawerOpen(false), drawerOpen);

  // 路由变化即关闭移动端抽屉（含浏览器前进/后退）
  useEffect(() => {
    setDrawerOpen(false);
  }, [location.pathname]);

  // 路由切换后把焦点移到主内容区：键盘/读屏用户不必再从顶栏重新 Tab 一遍。
  // 首次挂载不抢焦点（避免页面加载就出现焦点环）。
  const firstRender = useRef(true);
  useEffect(() => {
    if (firstRender.current) {
      firstRender.current = false;
      return;
    }
    mainRef.current?.focus();
  }, [location.pathname]);

  const logout = async () => {
    await fetch("/api/auth/logout", {
      method: "POST",
      credentials: "same-origin",
      // CSRF 纵深：写请求带自定义头（与 api/client.ts 一致）
      headers: { "X-Requested-With": "XMLHttpRequest" },
    });
    window.location.href = "/login";
  };

  return (
    <div className="flex min-h-full bg-[var(--surface)]">
      {/* 顶栏：全尺寸一致（汉堡仅移动端可见），账号与主题常驻右侧 */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex h-14 items-center gap-2 border-b border-border bg-[color:color-mix(in_oklch,var(--background)_88%,transparent)] px-3 backdrop-blur sm:px-5">
          <Button
            variant="ghost"
            size="icon"
            className="lg:hidden"
            aria-label="打开导航菜单"
            aria-expanded={drawerOpen}
            onClick={() => setDrawerOpen(true)}
          >
            <Menu />
          </Button>
          <Brand />
          <div className="ml-auto flex items-center gap-1">
            <Button variant="ghost" size="icon" asChild>
              <a
                href="https://github.com/RobbsLuo/coding2api"
                target="_blank"
                rel="noopener noreferrer"
                aria-label="项目仓库"
                title="项目仓库"
              >
                <GitBranch />
              </a>
            </Button>
            <ThemeToggle />
            <UserMenu
              username={session.username}
              role={session.role}
              onChangePassword={() => setChangingPassword(true)}
              onLogout={logout}
            />
          </div>
        </header>

        {/* 主体：桌面左侧导航栏 + 内容区（同一网格） */}
        <div className="mx-auto flex w-full max-w-[100rem] flex-1 items-start gap-6 px-3 py-6 sm:px-5 lg:px-8">
          <aside className="sticky top-20 hidden max-h-[calc(100vh-6rem)] w-52 shrink-0 self-start lg:flex lg:flex-col">
            <SidebarNav session={session} />
          </aside>
          <div className="flex min-w-0 flex-1 flex-col">
            <main ref={mainRef} tabIndex={-1} className="flex-1 focus:outline-none">
              <Outlet context={session} />
            </main>
            <footer className="pt-6 pb-2 text-xs text-muted-foreground">
              Coding2API · 仅供学习研究，未做安全审计
            </footer>
          </div>
        </div>
      </div>

      {/* 移动端抽屉导航 */}
      {drawerOpen && (
        <div className="fixed inset-0 z-50 flex lg:hidden" onClick={() => setDrawerOpen(false)}>
          <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" aria-hidden />
          <div
            ref={drawerRef}
            role="dialog"
            aria-modal="true"
            aria-label="导航菜单"
            data-testid="nav-drawer"
            onClick={(event) => event.stopPropagation()}
            className="relative flex h-full w-64 max-w-[80vw] flex-col border-r border-border bg-card shadow-lg"
          >
            <div className="flex h-14 items-center justify-between gap-2 border-b border-border px-4">
              <Brand />
              <Button
                variant="ghost"
                size="icon"
                aria-label="关闭导航"
                onClick={() => setDrawerOpen(false)}
              >
                <X />
              </Button>
            </div>
            <SidebarNav session={session} onNavigate={() => setDrawerOpen(false)} />
          </div>
        </div>
      )}

      {changingPassword && (
        <ChangePasswordDialog onCancel={() => setChangingPassword(false)} />
      )}
    </div>
  );
}

/** 页面通过 useOutletContext 取当前会话（含 is_admin，决定写操作可见性）。 */
export function useSessionContext(): SessionInfo {
  return useOutletContext<SessionInfo>();
}
