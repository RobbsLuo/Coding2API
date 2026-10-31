import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { Layout } from "../Layout";
import { notifyUnauthorized } from "../api/unauthorized";

function renderWithProviders(ui: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Layout", () => {
  it("管理员用户菜单显示角色与退出，且导航全量", async () => {
    renderWithProviders(<Layout session={{
      username: "root", is_operator: true, is_admin: true, role: "admin", must_change_password: false,
    }} />);
    // 导航始终可见
    expect(screen.getByText("凭证管理")).toBeInTheDocument();
    expect(screen.getByText("Playground")).toBeInTheDocument();
    // 角色与退出收在用户菜单（触发器只显示用户名+箭头）
    expect(screen.queryByText(/管理员/)).not.toBeInTheDocument();
    const trigger = screen.getByRole("button", { name: "用户菜单" });
    expect(trigger).not.toHaveTextContent("管理员");
    await userEvent.click(trigger);
    const menu = await screen.findByRole("menu");
    expect(menu).toHaveTextContent("root · 管理员");
    expect(menu).toHaveTextContent("退出");
  });

  it("普通用户展开用户菜单显示只读标记", async () => {
    renderWithProviders(<Layout session={{
      username: "guest", is_operator: false, is_admin: false, role: "viewer", must_change_password: false,
    }} />);
    await userEvent.click(screen.getByRole("button", { name: "用户菜单" }));
    const menu = await screen.findByRole("menu");
    expect(menu).toHaveTextContent("guest · 只读");
  });

  it("操作员角色显示操作员，管理组只露出上游状态与模型列表", async () => {
    renderWithProviders(<Layout session={{
      username: "op", is_operator: true, is_admin: false, role: "operator", must_change_password: false,
    }} />);
    // 凭证管理对操作员可见
    expect(screen.getByText("凭证管理")).toBeInTheDocument();
    // 用户管理/审计/告警/配置为 admin-only
    expect(screen.queryByText("用户管理")).not.toBeInTheDocument();
    expect(screen.queryByText("审计日志")).not.toBeInTheDocument();
    expect(screen.queryByText("运维告警")).not.toBeInTheDocument();
    expect(screen.queryByText("任务与配置")).not.toBeInTheDocument();
    // 上游状态与模型列表对操作员开放（与 require_operator 同门槛）
    expect(screen.getByText("上游状态")).toBeInTheDocument();
    expect(screen.getByText("模型列表")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "用户菜单" }));
    expect(await screen.findByRole("menu")).toHaveTextContent("op · 操作员");
  });

  it("只读用户看不到管理组与模型列表入口", async () => {
    renderWithProviders(<Layout session={{
      username: "guest", is_operator: false, is_admin: false, role: "viewer", must_change_password: false,
    }} />);
    expect(screen.getByText("凭证管理")).toBeInTheDocument();
    // 模型列表已移入管理组：viewer 不再看到入口
    expect(screen.queryByText("模型列表")).not.toBeInTheDocument();
    expect(screen.queryByText("上游状态")).not.toBeInTheDocument();
    expect(screen.queryByText("用户管理")).not.toBeInTheDocument();
  });

  it("管理员可在侧边栏看到管理类入口", async () => {
    renderWithProviders(<Layout session={{
      username: "root", is_operator: true, is_admin: true, role: "admin", must_change_password: false,
    }} />);
    // 管理类入口直接展示在侧边栏「管理」分组下（不再收进下拉）
    const nav = screen.getByRole("navigation", { name: "主导航" });
    expect(within(nav).getByText("用户管理")).toBeInTheDocument();
    expect(within(nav).getByText("审计日志")).toBeInTheDocument();
    expect(within(nav).getByText("运维告警")).toBeInTheDocument();
    expect(within(nav).getByText("上游状态")).toBeInTheDocument();
    expect(within(nav).getByText("模型列表")).toBeInTheDocument();
    expect(within(nav).getByText("任务与配置")).toBeInTheDocument();
    // 仓库入口已移出侧栏，改到顶栏图标按钮
    expect(within(nav).queryByText("项目仓库")).not.toBeInTheDocument();
  });

  it("项目仓库入口在顶栏，是带无障碍名的外链图标按钮", async () => {
    renderWithProviders(<Layout session={{
      username: "root", is_operator: true, is_admin: true, role: "admin", must_change_password: false,
    }} />);
    const link = screen.getByRole("link", { name: "项目仓库" });
    expect(link).toHaveAttribute("href", "https://github.com/RobbsLuo/coding2api");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("用户菜单可打开修改密码对话框", async () => {
    renderWithProviders(<Layout session={{
      username: "root", is_operator: true, is_admin: true, role: "admin", must_change_password: false,
    }} />);
    await userEvent.click(screen.getByRole("button", { name: "用户菜单" }));
    await userEvent.click(await screen.findByTestId("menu-change-password"));
    expect(await screen.findByTestId("change-password-dialog")).toBeInTheDocument();
  });

  it("退出：POST /api/auth/logout 后跳登录页", async () => {
    const fetchSpy = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        new Response(JSON.stringify({ ok: true }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchSpy);
    vi.stubGlobal("location", { href: "/" });
    renderWithProviders(<Layout session={{
      username: "root", is_operator: true, is_admin: true, role: "admin", must_change_password: false,
    }} />);
    await userEvent.click(screen.getByRole("button", { name: "用户菜单" }));
    await userEvent.click(await screen.findByTestId("menu-logout"));
    // 侧栏角标轮询也在用 fetch，按 URL 取那次登出调用（不假设它是第一次）
    await waitFor(() =>
      expect(fetchSpy.mock.calls.some((call) => call[0] === "/api/auth/logout")).toBe(true),
    );
    const [url, init] = fetchSpy.mock.calls.find(
      (call) => call[0] === "/api/auth/logout",
    ) as unknown as [string, RequestInit];
    expect(url).toBe("/api/auth/logout");
    expect((init.headers as Record<string, string>)["X-Requested-With"]).toBe("XMLHttpRequest");
    expect((globalThis as { location: { href: string } }).location.href).toBe("/login");
  });

  it("移动端抽屉：汉堡打开导航，Esc 关闭", async () => {
    renderWithProviders(<Layout session={{
      username: "root", is_operator: true, is_admin: true, role: "admin", must_change_password: false,
    }} />);
    // 初始不渲染抽屉
    expect(screen.queryByTestId("nav-drawer")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "打开导航菜单" }));
    const drawer = screen.getByTestId("nav-drawer");
    expect(within(drawer).getByText("任务与配置")).toBeInTheDocument();
    // 抽屉里的关闭按钮
    await userEvent.click(screen.getByRole("button", { name: "关闭导航" }));
    await waitFor(() => expect(screen.queryByTestId("nav-drawer")).not.toBeInTheDocument());
    // Esc 关闭
    await userEvent.click(screen.getByRole("button", { name: "打开导航菜单" }));
    expect(screen.getByTestId("nav-drawer")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("nav-drawer")).not.toBeInTheDocument());
  });

  it("移动端抽屉：点击遮罩关闭", async () => {
    renderWithProviders(<Layout session={{
      username: "root", is_operator: true, is_admin: true, role: "admin", must_change_password: false,
    }} />);
    await userEvent.click(screen.getByRole("button", { name: "打开导航菜单" }));
    const drawer = screen.getByTestId("nav-drawer");
    // 遮罩是抽屉的父容器，直接点父容器触发关闭
    await userEvent.click(drawer.parentElement as HTMLElement);
    await waitFor(() => expect(screen.queryByTestId("nav-drawer")).not.toBeInTheDocument());
  });
});
describe("App 路由守卫", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("未登录时重定向到登录页", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ error: { code: "unauthorized" } }), { status: 401 }),
      ),
    );
    renderWithProviders(<App />);
    expect(await screen.findByTestId("login-username")).toBeInTheDocument();
  });

  it("已登录时渲染凭证管理（首页）", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/auth/session") {
          return new Response(JSON.stringify({ username: "root", is_admin: true }), { status: 200 });
        }
        if (url === "/api/nav-badges") {
          return new Response(JSON.stringify({}), { status: 200 });
        }
        return new Response(JSON.stringify({ credentials: [], viewer: "root", is_admin: true }), {
          status: 200,
        });
      }),
    );
    renderWithProviders(<App />);
    expect(await screen.findByTestId("credentials-page")).toBeInTheDocument();
  });

  it("会话过期（任意页面 401）后回到登录页，而不是停在错误态", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/auth/session") {
          return new Response(JSON.stringify({
            username: "root", is_admin: true, is_operator: true, role: "admin",
            must_change_password: false,
          }), { status: 200 });
        }
        if (url === "/api/nav-badges") return new Response(JSON.stringify({}), { status: 200 });
        return new Response(JSON.stringify({ credentials: [], viewer: "root", is_admin: true }), {
          status: 200,
        });
      }),
    );
    renderWithProviders(<App />);
    expect(await screen.findByTestId("credentials-page")).toBeInTheDocument();

    // 模拟「停留期间会话被撤销」：某次请求 401 → client 通知 → App 清缓存回登录
    await act(async () => notifyUnauthorized());

    expect(await screen.findByTestId("login-username")).toBeInTheDocument();
    expect(screen.queryByTestId("credentials-page")).not.toBeInTheDocument();
  });

  it("会话加载中显示载入提示", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => undefined)));
    renderWithProviders(<App />);
    expect(screen.getByText("载入中…")).toBeInTheDocument();
  });

  it("首登未改密时只给强制改密对话框", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({
          username: "alice", is_operator: true, is_admin: false, role: "operator", must_change_password: true,
        }), { status: 200 }),
      ),
    );
    renderWithProviders(<App />);
    expect(await screen.findByTestId("change-password-dialog")).toBeInTheDocument();
  });

  it("未登录访问 /activate 直接渲染激活页（令牌即凭证）", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/auth/session")) {
        return new Response(JSON.stringify({ error: { code: "unauthorized" } }), { status: 401 });
      }
      return new Response(JSON.stringify({ username: "alice", valid: true }), { status: 200 });
    }));
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={["/activate?token=t"]}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(await screen.findByTestId("activate-page")).toBeInTheDocument();
    expect(await screen.findByText("alice")).toBeInTheDocument();
  });

  it("非 admin 直输 admin-only 路径被守卫送回首页", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/auth/session") {
          return new Response(JSON.stringify({
            username: "op", is_operator: true, is_admin: false, role: "operator",
            must_change_password: false,
          }), { status: 200 });
        }
        if (url === "/api/nav-badges") {
          return new Response(JSON.stringify({}), { status: 200 });
        }
        return new Response(JSON.stringify({ credentials: [], viewer: "op", is_admin: false }), {
          status: 200,
        });
      }),
    );
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={["/users"]}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    // 落到首页而不是用户管理
    expect(await screen.findByTestId("credentials-page")).toBeInTheDocument();
    expect(screen.queryByTestId("users-page")).not.toBeInTheDocument();
  });

  it("操作员可直输 /models 与 /upstream（与 require_operator 同门槛）", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/auth/session") {
          return new Response(JSON.stringify({
            username: "op", is_operator: true, is_admin: false, role: "operator",
            must_change_password: false,
          }), { status: 200 });
        }
        if (url === "/api/nav-badges") {
          return new Response(JSON.stringify({}), { status: 200 });
        }
        if (url.startsWith("/api/model-catalog")) {
          return new Response(JSON.stringify({
            models: [], count: 0, total: 0, currency: "USD", usd_cny_rate: 7, saved_at: null,
          }), { status: 200 });
        }
        return new Response(JSON.stringify({ credentials: [], viewer: "op", is_admin: false }), {
          status: 200,
        });
      }),
    );
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={["/models"]}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(await screen.findByTestId("model-catalog-page")).toBeInTheDocument();
  });

  it("只读用户直输 /models 被守卫送回首页", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/auth/session") {
          return new Response(JSON.stringify({
            username: "guest", is_operator: false, is_admin: false, role: "viewer",
            must_change_password: false,
          }), { status: 200 });
        }
        if (url === "/api/nav-badges") {
          return new Response(JSON.stringify({ api_keys: 0 }), { status: 200 });
        }
        return new Response(JSON.stringify({ credentials: [], viewer: "guest", is_admin: false }), {
          status: 200,
        });
      }),
    );
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={["/models"]}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(await screen.findByTestId("credentials-page")).toBeInTheDocument();
    expect(screen.queryByTestId("model-catalog-page")).not.toBeInTheDocument();
  });
});
