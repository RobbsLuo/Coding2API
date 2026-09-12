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

/** 在 Layout 下渲染页面，让 useSessionContext 拿到会话。 */
export function renderPage(ui: ReactNode, session: SessionInfo = { username: "root", is_admin: true }) {
  return render(
    <QueryClientProvider client={makeClient()}>
      <MemoryRouter initialEntries={["/page"]}>
        <Routes>
          <Route element={<Layout session={session} />}>
            <Route path="/page" element={ui} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** 独立渲染（登录页不需要 Layout）。 */
export function renderStandalone(ui: ReactNode) {
  return render(
    <QueryClientProvider client={makeClient()}>
      <MemoryRouter>{ui}</MemoryRouter>
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
    quota_probed_at: 1_700_000_000,
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
