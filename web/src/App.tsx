import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "./Layout";
import { useSession } from "./api/hooks";
import { ChangePasswordDialog } from "./components/ChangePasswordDialog";
import { ActivatePage } from "./pages/ActivatePage";
import { LoginPage } from "./pages/LoginPage";

// 路由级懒加载：recharts / 大页面不进首屏 chunk（900KB 单 chunk → 拆包）
const CredentialsPage = lazy(() => import("./pages/CredentialsPage").then(m => ({ default: m.CredentialsPage })));
const DashboardPage = lazy(() => import("./pages/DashboardPage").then(m => ({ default: m.DashboardPage })));
const ApiKeysPage = lazy(() => import("./pages/ApiKeysPage").then(m => ({ default: m.ApiKeysPage })));
const StatsPage = lazy(() => import("./pages/StatsPage").then(m => ({ default: m.StatsPage })));
const PlaygroundPage = lazy(() => import("./pages/PlaygroundPage").then(m => ({ default: m.PlaygroundPage })));
const SettingsPage = lazy(() => import("./pages/SettingsPage").then(m => ({ default: m.SettingsPage })));
const UsersPage = lazy(() => import("./pages/UsersPage").then(m => ({ default: m.UsersPage })));
const AuditPage = lazy(() => import("./pages/AuditPage").then(m => ({ default: m.AuditPage })));

const LOADING = (
  <div className="grid h-full place-items-center text-sm text-[var(--color-ink-muted)]">载入中…</div>
);

export function App() {
  const session = useSession();

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
    <Suspense fallback={LOADING}>
      <Routes>
        <Route path="/login" element={<Navigate to="/" replace />} />
        <Route element={<Layout session={session.data} />}>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/credentials" element={<CredentialsPage />} />
          <Route path="/api-keys" element={<ApiKeysPage />} />
          <Route path="/stats" element={<StatsPage />} />
          <Route path="/playground" element={<PlaygroundPage />} />
          <Route path="/users" element={<UsersPage />} />
          <Route path="/audit" element={<AuditPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Suspense>
  );
}
