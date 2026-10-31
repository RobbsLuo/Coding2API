import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, REQUEST_TIMEOUT_MS } from "../client";
import { setUnauthorizedHandler } from "../unauthorized";

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

    await api.upstreamStart("codebuddy");
    await api.upstreamPoll("codebuddy", "s");
    await api.upstreamCancel("codebuddy", "s");
    await api.probeCredential("c1");
    await api.checkinCredential("c1");
    await api.apiKeys();
    await api.createApiKey({ name: "n", provider_binding: "trae", allowed_ips: "10.0.0.0/8" });
    await api.deleteApiKey("k1");
    await api.statsByProvider("alice", 5);
    await api.logout();
  });

  it("上游状态快照：GET /api/upstream-status，无查询参数", async () => {
    const spy = vi.fn(async () => new Response(JSON.stringify({ sources: [] }), { status: 200 }));
    vi.stubGlobal("fetch", spy);

    await api.upstreamStatus();

    expect(spy).toHaveBeenCalledWith(
      "/api/upstream-status",
      expect.objectContaining({ credentials: "same-origin" }),
    );
  });

  it("错误体不是 JSON（反代 HTML 页）时仍保留真实 HTTP 状态", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("<html>502 Bad Gateway</html>", {
        status: 502,
        statusText: "Bad Gateway",
      })),
    );
    const error = await api.credentials().catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(502);
    expect((error as ApiError).code).toBe("unknown");
    // 兜底文案不能是空串：至少给 statusText，页面才不会弹一个空白提示
    expect((error as ApiError).message).toBe("Bad Gateway");
  });

  it("请求超时归一为 ApiError(timeout)，不把页面挂在 loading", async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      "fetch",
      vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("aborted", "AbortError")),
          );
        })),
    );
    // 先挂上断言再推进定时器：否则 rejection 先发生、处理器后挂上，
    // vitest 会把这次「超时」记成未处理拒绝（假报错）。
    const assertion = expect(api.credentials()).rejects.toMatchObject({
      name: "ApiError",
      status: 0,
      code: "timeout",
    });
    await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS);
    await assertion;
    vi.useRealTimers();
  });

  it("调用方主动取消时原样上抛 AbortError（不被误判为超时）", async () => {
    const controller = new AbortController();
    vi.stubGlobal(
      "fetch",
      vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("aborted", "AbortError")),
          );
        })),
    );
    const pending = api.playgroundModels(controller.signal);
    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  });

  it("受保护端点 401 触发会话过期处理器", async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ error: { code: "unauthorized" } }), { status: 401 }),
      ),
    );

    await expect(api.users()).rejects.toMatchObject({ status: 401 });
    expect(handler).toHaveBeenCalledTimes(1);
    setUnauthorizedHandler(null);
  });

  it("登录/会话端点的 401 不触发会话过期（密码错不该踢人）", async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ error: { code: "unauthorized" } }), { status: 401 }),
      ),
    );

    await expect(api.login("root", "wrong")).rejects.toMatchObject({ status: 401 });
    await expect(api.session()).rejects.toMatchObject({ status: 401 });
    expect(handler).not.toHaveBeenCalled();
    setUnauthorizedHandler(null);
  });
});
