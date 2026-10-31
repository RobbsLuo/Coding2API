import { useEffect, lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { Layout } from "./Layout";
import { useSession } from "./api/hooks";
import { setUnauthorizedHandler } from "./api/unauthorized";
import { ChangePasswordDialog } from "./components/ChangePasswordDialog";
import { FirstRunWizard } from "./components/FirstRunWizard";
import { ActivatePage } from "./pages/ActivatePage";
import { LoginPage } from "./pages/LoginPage";

// 路由级懒加载：recharts / 大页面不进首屏 chunk（900KB 单 chunk → 拆包）
const CredentialsPage = lazy(() => import("./pages/CredentialsPage").then(m => ({ default: m.CredentialsPage })));
const ApiKeysPage = lazy(() => import("./pages/ApiKeysPage").then(m => ({ default: m.ApiKeysPage })));
const StatsPage = lazy(() => import("./pages/StatsPage").then(m => ({ default: m.StatsPage })));
const ModelCatalogPage = lazy(() => import("./pages/ModelCatalogPage").then(m => ({ default: m.ModelCatalogPage })));
const PlaygroundPage = lazy(() => import("./pages/PlaygroundPage").then(m => ({ default: m.PlaygroundPage })));
const SettingsPage = lazy(() => import("./pages/SettingsPage").then(m => ({ default: m.SettingsPage })));
const UsersPage = lazy(() => import("./pages/UsersPage").then(m => ({ default: m.UsersPage })));
const AuditPage = lazy(() => import("./pages/AuditPage").then(m => ({ default: m.AuditPage })));
const AlertsPage = lazy(() => import("./pages/AlertsPage").then(m => ({ default: m.AlertsPage })));
const BackupsPage = lazy(() => import("./pages/BackupsPage").then(m => ({ default: m.BackupsPage })));
const LogsPage = lazy(() => import("./pages/LogsPage").then(m => ({ default: m.LogsPage })));
const UpstreamStatusPage = lazy(() => import("./pages/UpstreamStatusPage").then(m => ({ default: m.UpstreamStatusPage })));
const BillingPage = lazy(() => import("./pages/BillingPage").then(m => ({ default: m.BillingPage })));

const LOADING = (
  <div className="grid h-full place-items-center text-sm text-[var(--color-ink-muted)]">载入中…</div>
);

/**
 * admin-only 页面的路由守卫：非 admin 直接送回首页。
 *
 * 侧栏已隐藏这些入口，但地址栏直输 / 浏览器历史仍能到达；后端会 403，
 * 用户看到的却是一片「无权限」报错。这里前置拦截，与侧栏可见性同一口径。
 */
function RequireAdmin({ isAdmin, children }: { isAdmin: boolean; children: React.ReactNode }) {
  if (!isAdmin) return <Navigate to="/" replace />;
  return <>{children}</>;
}

/** 操作员（含 admin）页面的路由守卫：viewer 送回首页。与侧栏 `operator` 项同口径。 */
function RequireOperator({ isOperator, children }: {
  isOperator: boolean;
  children: React.ReactNode;
}) {
  if (!isOperator) return <Navigate to="/" replace />;
  return <>{children}</>;
}

export function App() {
  const session = useSession();
  const queryClient = useQueryClient();

  // 会话过期（任意页面请求 401）→ 清掉带权限的缓存并让 session 立即变「未登录」，
  // 下面的分支随之渲染登录页。不做整页跳转：路由本来就跟 session 数据走，
  // 而 window.location 跳转会丢 SPA 状态且绕开 MemoryRouter（测试里也会炸）。
  useEffect(() => {
    setUnauthorizedHandler(() => {
      // 只丢受保护数据，保留 session key 并显式置空：删掉 session 查询会让
      // observer 立刻重取，而 Cookie 已经失效，等于白发一次 401 请求。
      queryClient.removeQueries({
        predicate: (query) => query.queryKey[0] !== "session",
      });
      queryClient.setQueryData(["session"], null);
    });
    return () => setUnauthorizedHandler(null);
  }, [queryClient]);

  if (session.isLoading) {
    return LOADING;
  }

  if (!session.data) {
    return (
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        {/* 激活页无需登录：用户拿到的是一次性令牌，还没有可用密码 */}
        <Route path="/activate" element={<ActivatePage />} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    );
  }

  // 首登/被重置后强制改密：其余页面都会被后端 403，直接只给改密对话框，
  // 避免用户点开任何页面看到一片报错。
  if (session.data.must_change_password) {
    return <ChangePasswordDialog required />;
  }

  return (
    <>
      <FirstRunWizard username={session.data.username} isAdmin={session.data.is_admin} />
      <Suspense fallback={LOADING}>
      <Routes>
        <Route path="/login" element={<Navigate to="/" replace />} />
        <Route element={<Layout session={session.data} />}>
          <Route path="/" element={<CredentialsPage />} />
          <Route path="/credentials" element={<Navigate to="/" replace />} />
          <Route path="/api-keys" element={<ApiKeysPage />} />
          <Route path="/stats" element={<StatsPage />} />
          <Route path="/playground" element={<PlaygroundPage />} />
          <Route path="/users" element={<RequireAdmin isAdmin={session.data.is_admin}><UsersPage /></RequireAdmin>} />
          <Route path="/audit" element={<RequireAdmin isAdmin={session.data.is_admin}><AuditPage /></RequireAdmin>} />
          <Route path="/logs" element={<RequireAdmin isAdmin={session.data.is_admin}><LogsPage /></RequireAdmin>} />
          <Route path="/alerts" element={<RequireAdmin isAdmin={session.data.is_admin}><AlertsPage /></RequireAdmin>} />
          <Route path="/backups" element={<RequireAdmin isAdmin={session.data.is_admin}><BackupsPage /></RequireAdmin>} />
          <Route path="/upstream" element={<RequireOperator isOperator={session.data.is_operator}><UpstreamStatusPage /></RequireOperator>} />
          <Route path="/billing" element={<RequireOperator isOperator={session.data.is_operator}><BillingPage /></RequireOperator>} />
          <Route path="/models" element={<RequireOperator isOperator={session.data.is_operator}><ModelCatalogPage /></RequireOperator>} />
          <Route path="/settings" element={<RequireAdmin isAdmin={session.data.is_admin}><SettingsPage /></RequireAdmin>} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
      </Suspense>
    </>
  );
}
