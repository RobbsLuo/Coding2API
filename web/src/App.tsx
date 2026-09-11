import { Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "./Layout";
import { useSession } from "./api/hooks";
import { LoginPage } from "./pages/LoginPage";
import { CredentialsPage } from "./pages/CredentialsPage";
import { DashboardPage } from "./pages/DashboardPage";
import { ApiKeysPage } from "./pages/ApiKeysPage";
import { StatsPage } from "./pages/StatsPage";
import { PlaygroundPage } from "./pages/PlaygroundPage";

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
  );
}
