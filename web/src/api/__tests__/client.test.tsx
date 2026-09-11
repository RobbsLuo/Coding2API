import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../../App";
import { Layout } from "../../Layout";
import { api, ApiError } from "../client";
import {
  credentialState,
  formatDuration,
  formatNumber,
  formatTime,
  healthView,
  probeFailureLabel,
  quotaSemantics,
} from "../display";
import { makeCredential } from "../../pages/__tests__/helpers";

function renderWithProviders(ui: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("api client", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("解析错误响应为 ApiError 并保留 code", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ error: { code: "no_healthy_credential", message: "全挂了" } }), {
          status: 503,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    await expect(api.credentials()).rejects.toMatchObject({
      name: "ApiError",
      status: 503,
      code: "no_healthy_credential",
      message: "全挂了",
    });
  });

  it("错误响应缺少 error 字段时回退到 statusText", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("", { status: 502 })));
    const error = await api.credentials().catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).code).toBe("unknown");
  });

  it("空响应体返回 null 而不抛解析错误", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(null, { status: 200 })));
    await expect(api.session()).resolves.toBeNull();
  });

  it("query 构造：跳过空值，保留数字", async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        calls.push(String(input));
        return new Response(JSON.stringify({}), { status: 200 });
      }),
    );
    await api.statsOverview("alice", 123);
    expect(calls[0]).toBe("/api/stats/overview?username=alice&since=123");
  });

  it("query 构造：无参数时不留问号", async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        calls.push(String(input));
        return new Response(JSON.stringify({}), { status: 200 });
      }),
    );
    await api.statsOverview();
    expect(calls[0]).toBe("/api/stats/overview");
  });

  it("写操作使用正确的 HTTP 方法与 JSON 头", async () => {
    const spy = vi.fn(async () => new Response(JSON.stringify({ ok: true }), { status: 200 }));
    vi.stubGlobal("fetch", spy);

    await api.toggleCredential("c1", false);
    expect(spy).toHaveBeenCalledWith(
      "/api/credentials/c1/toggle",
      expect.objectContaining({ method: "POST" }),
    );

    await api.deleteCredential("c1");
    expect(spy).toHaveBeenCalledWith("/api/credentials/c1", expect.objectContaining({ method: "DELETE" }));

    await api.pinCredential(null);
    expect(spy).toHaveBeenCalledWith(
      "/api/credentials/pin",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ credential_id: null }) }),
    );

    await api.importCredential("codebuddy", { token: "t" }, "昵称");
    expect(spy).toHaveBeenCalledWith(
      "/api/credentials",
      expect.objectContaining({ method: "POST" }),
    );

    await api.selectAccount("c1", "a1");
    expect(spy).toHaveBeenCalledWith(
      "/api/credentials/c1/accounts/select",
      expect.objectContaining({ method: "POST" }),
    );

    await api.upstreamStart("codebuddy");
    await api.upstreamPoll("codebuddy", "s");
    await api.upstreamCancel("codebuddy", "s");
    await api.probeCredential("c1");
    await api.checkinCredential("c1");
    await api.accounts("c1");
    await api.apiKeys();
    await api.createApiKey("n");
    await api.deleteApiKey("k1");
    await api.statsByProvider("alice", 5);
    await api.logout();
  });

  it("models 与 chatCompletion 使用 Bearer 头", async () => {
    const spy = vi.fn(async () => new Response(JSON.stringify({ object: "list", data: [] }), { status: 200 }));
    vi.stubGlobal("fetch", spy);

    await api.models("sk-abc");
    expect(spy).toHaveBeenCalledWith(
      "/v1/models",
      expect.objectContaining({ headers: { Authorization: "Bearer sk-abc" } }),
    );

    const calls: { url: string; init?: RequestInit }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({ url: String(input), init });
        return new Response(JSON.stringify({ object: "list", data: [] }), { status: 200 });
      }),
    );
    await api.chatCompletion("sk-abc", { model: "m" });
    const chat = calls.at(-1)!;
    expect(chat.url).toBe("/v1/chat/completions");
    expect(chat.init?.headers).toMatchObject({ Authorization: "Bearer sk-abc" });


  });
});

describe("display helpers", () => {
  it("健康度三态互不混淆", () => {
    expect(healthView(null)).toMatchObject({ kind: "unknown", label: "未探测到额度", tone: "muted" });
    expect(healthView(-1)).toMatchObject({ kind: "exhausted", label: "已耗尽", tone: "danger" });
    expect(healthView(0)).toMatchObject({ kind: "known", percent: 0, tone: "danger" });
    expect(healthView(30)).toMatchObject({ kind: "known", tone: "warn" });
    expect(healthView(90)).toMatchObject({ kind: "known", tone: "ok" });
  });

  it("undefined 也视为未探测", () => {
    expect(healthView(undefined as unknown as null).kind).toBe("unknown");
  });

  it("周期语义按上游类型判定，而不是看 cycle_end 是否存在", () => {
    // CodeBuddy 有重置时间
    const cycle = quotaSemantics(makeCredential({
      provider: "codebuddy", quota_cycle_end: 1_800_000_000 }) as never);
    expect(cycle).toContain("本周期剩余，");
    expect(cycle).toContain("重置");

    // CodeBuddy 未探测：cycle_end 为 null，但绝不能显示成 TRAE 的语义
    const unprobed = quotaSemantics(makeCredential({
      provider: "codebuddy", quota_cycle_end: null }) as never);
    expect(unprobed).toContain("本周期剩余");
    expect(unprobed).not.toContain("单调递减");

    // TRAE 永远是单调递减
    const balance = quotaSemantics(makeCredential({
      provider: "trae", quota_cycle_end: null }) as never);
    expect(balance).toBe("账户剩余（单调递减）");
  });

  it("凭证状态机覆盖全部状态", () => {
    expect(credentialState(makeCredential() as never, 0)).toBe("ready");
    expect(credentialState(makeCredential({ disabled: 1 }) as never, 0)).toBe("disabled");
    expect(credentialState(makeCredential({ enabled: 0 }) as never, 0)).toBe("off");
    expect(credentialState(makeCredential({ cooling_until: 500 }) as never, 100)).toBe("cooling");
    expect(credentialState(makeCredential({ health: -1 }) as never, 0)).toBe("exhausted");
    // 冷却已过期 → 不再是 cooling
    expect(credentialState(makeCredential({ cooling_until: 50 }) as never, 100)).toBe("ready");
  });

  it("时长格式化覆盖秒/分/时/天与零值", () => {
    expect(formatDuration(0)).toBe("—");
    expect(formatDuration(-5)).toBe("—");
    expect(formatDuration(30)).toBe("30 秒");
    expect(formatDuration(120)).toBe("2 分钟");
    expect(formatDuration(7200)).toBe("2.0 小时");
    expect(formatDuration(172800)).toBe("2.0 天");
  });

  it("探测失败原因有中文文案，且未知值有兜底", () => {
    expect(probeFailureLabel("credential_rejected")).toContain("重新登录");
    expect(probeFailureLabel("upstream_unavailable")).toContain("与本账号凭证无关");
    expect(probeFailureLabel(undefined)).toBe("未知错误");
    expect(probeFailureLabel("something_new" as never)).toBe("未知错误");
  });

  it("时间与数字格式化处理空值", () => {
    expect(formatTime(null)).toBe("—");
    expect(formatTime(1_700_000_000)).not.toBe("—");
    expect(formatNumber(null)).toBe("—");
    expect(formatNumber(1234)).toBe("1,234");
  });
});

describe("Layout", () => {
  it("管理员用户菜单显示角色与退出，且导航全量", async () => {
    renderWithProviders(<Layout session={{ username: "root", is_admin: true }} />);
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
    renderWithProviders(<Layout session={{ username: "guest", is_admin: false }} />);
    await userEvent.click(screen.getByRole("button", { name: "用户菜单" }));
    const menu = await screen.findByRole("menu");
    expect(menu).toHaveTextContent("guest · 只读");
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

  it("已登录时渲染仪表盘", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/auth/session") {
          return new Response(JSON.stringify({ username: "root", is_admin: true }), { status: 200 });
        }
        return new Response(JSON.stringify({ credentials: [], viewer: "root", is_admin: true }), {
          status: 200,
        });
      }),
    );
    renderWithProviders(<App />);
    expect(await screen.findByTestId("dashboard")).toBeInTheDocument();
  });

  it("会话加载中显示载入提示", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => undefined)));
    renderWithProviders(<App />);
    expect(screen.getByText("载入中…")).toBeInTheDocument();
  });
});
