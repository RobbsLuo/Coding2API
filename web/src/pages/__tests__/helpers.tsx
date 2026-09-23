import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { ReactNode } from "react";
import { vi } from "vitest";
import { Layout } from "../../Layout";
import type { SessionInfo } from "../../api/types";

export function makeClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
}

/**
 * 测试会话：只需给 username（+ 可选的升级字段）。
 *
 * B5 给 SessionInfo 加了 role / must_change_password，历史上几十处调用点
 * 只传 `{ username, is_admin }`。这里按 is_admin 推导 role 并补默认值，
 * 避免为了两个新字段去改所有测试（也顺带让老测试继续验证向后兼容）。
 */
export type TestSession = Pick<SessionInfo, "username"> &
  Partial<Omit<SessionInfo, "username">>;

export function fullSession(session: TestSession): SessionInfo {
  const { role, ...rest } = session;
  return {
    must_change_password: false,
    is_admin: rest.is_admin ?? false,
    ...rest,
    role: role ?? (session.is_admin ? "admin" : "viewer"),
  } as SessionInfo;
}

/** 在 Layout 下渲染页面，让 useSessionContext 拿到会话。 */
export function renderPage(ui: ReactNode, session: TestSession = { username: "root", is_admin: true }) {
  return render(
    <QueryClientProvider client={makeClient()}>
      <MemoryRouter initialEntries={["/page"]}>
        <Routes>
          <Route element={<Layout session={fullSession(session)} />}>
            <Route path="/page" element={ui} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** 独立渲染（登录页/激活页不需要 Layout）。 */
export function renderStandalone(ui: ReactNode, path = "/") {
  return render(
    <QueryClientProvider client={makeClient()}>
      <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** 按 URL 分派的 fetch mock；函数路由会收到完整 URL 字符串。 */
export function mockFetch(routes: Record<string, unknown | ((url: string) => Response)>) {
  const spy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    for (const [pattern, value] of Object.entries(routes)) {
      if (url.includes(pattern)) {
        if (typeof value === "function") return (value as (url: string) => Response)(url);
        return jsonResponse(value);
      }
    }
    throw new Error(`未 mock 的请求: ${init?.method ?? "GET"} ${url}`);
  });
  vi.stubGlobal("fetch", spy);
  return spy;
}

export function makeCredential(overrides: Record<string, unknown> = {}) {
  return {
    id: "cred_1",
    provider: "trae",
    nickname: "主账号",
    enabled: 1,
    disabled: 0,
    disabled_reason: null,
    pinned: 0,
    health: 62,
    cooling_until: null,
    err_count: 0,
    quota_remaining: 62,
    quota_total: 100,
    quota_cycle_end: null,
    quota_expiring_credits: null,
    quota_expiring_credits_secondary: null,
    quota_expiry_ladder: null,
    quota_packages: null,
    model_cooldowns: [],
    quota_probed_at: 1_700_000_000,
    token_expires_at: 0,
    token_issued_at: 0,
    growth_last_run_at: null,
    growth_last_result: null,
    created_at: 1_700_000_000,
    added_by: "root",
    ...overrides,
  };
}

export async function settle() {
  await waitFor(() => {
    expect(screen.queryByText("载入中…")).not.toBeInTheDocument();
  });
}

export { userEvent };
