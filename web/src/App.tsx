import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "./Layout";
import { useSession } from "./api/hooks";
import { LoginPage } from "./pages/LoginPage";

// 路由级懒加载：recharts / 大页面不进首屏 chunk（900KB 单 chunk → 拆包）
const CredentialsPage = lazy(() => import("./pages/CredentialsPage").then(m => ({ default: m.CredentialsPage })));
const DashboardPage = lazy(() => import("./pages/DashboardPage").then(m => ({ default: m.DashboardPage })));
const ApiKeysPage = lazy(() => import("./pages/ApiKeysPage").then(m => ({ default: m.ApiKeysPage })));
const StatsPage = lazy(() => import("./pages/StatsPage").then(m => ({ default: m.StatsPage })));
const PlaygroundPage = lazy(() => import("./pages/PlaygroundPage").then(m => ({ default: m.PlaygroundPage })));

export function App() {
  const session = useSession();

  if (session.isLoading) {
    return <div className="grid h-full place-items-center text-sm text-[var(--color-ink-muted)]">载入中…</div>;
  }

  if (!session.data) {
    return (
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    );
  }

  return (
    <Suspense fallback={<div className="grid h-full place-items-center text-sm text-[var(--color-ink-muted)]">载入中…</div>}>
      <Routes>
        <Route path="/login" element={<Navigate to="/" replace />} />
        <Route element={<Layout session={session.data} />}>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/credentials" element={<CredentialsPage />} />
          <Route path="/api-keys" element={<ApiKeysPage />} />
          <Route path="/stats" element={<StatsPage />} />
          <Route path="/playground" element={<PlaygroundPage />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Suspense>
  );
}
