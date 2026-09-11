import { screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CredentialsPage } from "../CredentialsPage";
import { jsonResponse, makeCredential, mockFetch, renderPage, settle, userEvent } from "./helpers";

const ADMIN = { username: "root", is_admin: true } as const;
const READER = { username: "guest", is_admin: false } as const;

function listBody(credentials: unknown[], isAdmin = true) {
  return { credentials, viewer: isAdmin ? "root" : "guest", is_admin: isAdmin };
}

describe("CredentialsPage", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("渲染凭证列表与三态健康度", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ id: "a", health: 62 }),
        makeCredential({ id: "b", nickname: "未探测号", health: null }),
        makeCredential({ id: "c", nickname: "耗尽号", health: -1 }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    const table = screen.getByTestId("credentials-table");
    expect(within(table).getByText("62%")).toBeInTheDocument();
    expect(within(table).getByText("未探测到额度")).toBeInTheDocument();
    expect(within(table).getByText("已耗尽")).toBeInTheDocument();
  });

  it("非管理员隐藏所有写操作并显示只读横幅", async () => {
    mockFetch({ "/api/credentials": listBody([makeCredential()], false) });
    renderPage(<CredentialsPage />, READER);
    await settle();

    expect(screen.getByTestId("readonly-banner")).toHaveTextContent("只读模式");
    expect(screen.queryByRole("button", { name: "探测" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "签到" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "删除" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("import-submit")).not.toBeInTheDocument();
    expect(screen.queryByTestId("start-login")).not.toBeInTheDocument();
  });

  it("冷却中的凭证显示剩余时间", async () => {
    const future = Math.floor(Date.now() / 1000) + 7200;
    mockFetch({
      "/api/credentials": listBody([makeCredential({ cooling_until: future })]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    const row = screen.getByTestId(/^row-/);
    expect(within(row).getByText("冷却中")).toBeInTheDocument();
    expect(within(row).getByText(/小时/)).toBeInTheDocument();
  });

  it("启用/停用调用 toggle 接口", async () => {
    const fetchSpy = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/toggle")) return jsonResponse({ ok: true });
      return jsonResponse(listBody([makeCredential({ id: "cred_1", enabled: 1 })]));
    });
    vi.stubGlobal("fetch", fetchSpy);

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByRole("button", { name: "停用" }));

    await waitFor(() =>
      expect(fetchSpy).toHaveBeenCalledWith(
        "/api/credentials/cred_1/toggle",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("指定优先使用调用 pin 接口", async () => {
    const fetchSpy = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/pin")) return jsonResponse({ ok: true });
      return jsonResponse(listBody([makeCredential({ id: "cred_1", pinned: 0 })]));
    });
    vi.stubGlobal("fetch", fetchSpy);

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByRole("button", { name: "指定" }));

    await waitFor(() =>
      expect(fetchSpy).toHaveBeenCalledWith(
        "/api/credentials/pin",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("删除需要二次确认", async () => {
    const fetchSpy = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "DELETE") return jsonResponse({ ok: true });
      return jsonResponse(listBody([makeCredential({ id: "cred_1" })]));
    });
    vi.stubGlobal("fetch", fetchSpy);

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByRole("button", { name: "删除" }));
    expect(screen.getByRole("button", { name: "确认删除" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "确认删除" }));
    await waitFor(() =>
      expect(fetchSpy).toHaveBeenCalledWith(
        "/api/credentials/cred_1",
        expect.objectContaining({ method: "DELETE" }),
      ),
    );
  });

  it("导入凭证：非法 JSON 本地拦截，不发起请求", async () => {
    const fetchSpy = mockFetch({
      "/api/credentials": listBody([]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    await userEvent.type(screen.getByTestId("import-payload"), "not-json-at-all");
    await userEvent.click(screen.getByTestId("import-submit"));

    expect(screen.getByText(/必须是合法 JSON/)).toBeInTheDocument();
    expect(fetchSpy).toHaveBeenCalledTimes(1); // 只有列表请求
  });

  it("导入凭证：合法 JSON 调用导入接口", async () => {
    const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/credentials" && init?.method === "POST") {
        return jsonResponse({ id: "cred_new" });
      }
      return jsonResponse(listBody([]));
    });
    vi.stubGlobal("fetch", fetchSpy);

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    // 用 paste 写入 JSON，避开 userEvent.type 对花括号的转义语义
    const payload = screen.getByTestId("import-payload");
    await userEvent.click(payload);
    await userEvent.paste('{"token":"abc"}');
    await userEvent.click(screen.getByTestId("import-submit"));

    await waitFor(() =>
      expect(fetchSpy).toHaveBeenCalledWith(
        "/api/credentials",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("探测失败时提示「未探测」而非额度为 0，并给出可操作原因", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/probe")) {
          return jsonResponse({
            probed: false,
            reason: "upstream_response_invalid",
            detail: "quota response missing Accounts",
          });
        }
        return jsonResponse(listBody([makeCredential({ id: "cred_1" })]));
      }),
    );

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByRole("button", { name: "探测" }));

    const notice = await screen.findByTestId("credentials-notice");
    expect(notice).toHaveTextContent("探测失败");
    expect(notice).toHaveTextContent("未探测");
    // 面向用户的是可操作的中文说明，不是枚举或异常类名
    expect(notice).toHaveTextContent("上游响应格式与预期不符");
    expect(notice).not.toHaveTextContent("upstream_response_invalid");
    // 原始错误另置于折叠区，便于排查
    expect(screen.getByTestId("probe-detail")).toHaveTextContent(
      "quota response missing Accounts",
    );
  });

  it("探测失败的未知原因有兜底文案", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/probe")) return jsonResponse({ probed: false });
        return jsonResponse(listBody([makeCredential({ id: "cred_1" })]));
      }),
    );

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByRole("button", { name: "探测" }));
    expect(await screen.findByTestId("credentials-notice")).toHaveTextContent("未知错误");
  });

  it("探测成功展示剩余与总量", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/probe")) {
          return jsonResponse({ probed: true, remaining: 30, total: 100 });
        }
        return jsonResponse(listBody([makeCredential({ id: "cred_1" })]));
      }),
    );

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByRole("button", { name: "探测" }));

    const notice = await screen.findByTestId("credentials-notice");
    expect(notice).toHaveTextContent("探测成功");
    expect(notice).toHaveTextContent("30");
  });

  it("签到成功展示获得积分", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/checkin")) {
          return jsonResponse({ ok: true, credit: 100, code: 0, message: "" });
        }
        return jsonResponse(listBody([makeCredential({ id: "cred_1" })]));
      }),
    );

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByRole("button", { name: "签到" }));
    expect(await screen.findByTestId("credentials-notice")).toHaveTextContent("100");
  });

  it("已签到时显示上游原文，不出现「获得 — 积分」", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/checkin")) {
          return jsonResponse({
            ok: true, credit: null, code: 10001,
            message: "今天已签到，请明天再来", already_checked_in: true,
          });
        }
        return jsonResponse(listBody([makeCredential({ id: "cred_1" })]));
      }),
    );

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByRole("button", { name: "签到" }));
    const notice = await screen.findByTestId("credentials-notice");
    expect(notice).toHaveTextContent("今天已签到，请明天再来");
    expect(notice).not.toHaveTextContent("获得");
  });

  it("签到成功但无 credit 时显示「签到成功」", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/checkin")) {
          return jsonResponse({ ok: true, credit: null, code: 0,
                                message: "", already_checked_in: false });
        }
        return jsonResponse(listBody([makeCredential({ id: "cred_1" })]));
      }),
    );

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByRole("button", { name: "签到" }));
    expect(await screen.findByTestId("credentials-notice")).toHaveTextContent("签到成功");
  });

  it("签到未成功展示 code，且 code 为 null 时也能渲染", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/checkin")) {
          return jsonResponse({ ok: false, credit: null, code: null, message: "已签到" });
        }
        return jsonResponse(listBody([makeCredential({ id: "cred_1" })]));
      }),
    );

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByRole("button", { name: "签到" }));
    expect(await screen.findByTestId("credentials-notice")).toHaveTextContent("code=null");
  });

  it("账号切换：拉列表并可切换", async () => {
    const fetchSpy = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/accounts/select")) return jsonResponse({ switched: true });
      if (url.includes("/accounts")) {
        return jsonResponse({
          accounts: [{ account_id: "acct_1", nickname: "第二个", type: "personal" }],
        });
      }
      return jsonResponse(listBody([makeCredential({ id: "cred_1" })]));
    });
    vi.stubGlobal("fetch", fetchSpy);

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByRole("button", { name: "账号" }));

    const list = await screen.findByTestId("accounts-list");
    expect(list).toHaveTextContent("第二个");

    await userEvent.click(screen.getByRole("button", { name: "切换到此账号" }));
    await waitFor(() =>
      expect(fetchSpy).toHaveBeenCalledWith(
        "/api/credentials/cred_1/accounts/select",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("空池提示", async () => {
    mockFetch({ "/api/credentials": listBody([]) });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    expect(screen.getByTestId("no-credentials")).toBeInTheDocument();
  });
});


describe("上游登录入口", () => {
  it("两个上游都能发起登录", async () => {
    mockFetch({ "/api/credentials": listBody([]) });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    expect(screen.getByTestId("start-login-codebuddy")).toHaveTextContent("登录 CodeBuddy");
    expect(screen.getByTestId("start-login-trae")).toHaveTextContent("登录 TRAE");
  });

  it("CodeBuddy 走 poll：打开授权页并轮询上游", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const opened: string[] = [];
    vi.stubGlobal("open", (url: string) => {
      opened.push(url);
      return null;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/upstream/start")) {
          return jsonResponse({
            flow: "poll",
            state: "res-1",
            auth_url: "https://auth.example/x",
            interval: 5,
            callback_url: null,
          });
        }
        if (url.includes("/upstream/poll")) {
          return jsonResponse({ status: "success", credential_id: "cred_new" });
        }
        return jsonResponse(listBody([]));
      }),
    );

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByTestId("start-login-codebuddy"));

    expect(opened).toEqual(["https://auth.example/x"]);
    expect(await screen.findByTestId("cancel-login-codebuddy")).toBeInTheDocument();

    await vi.advanceTimersByTimeAsync(6000);
    expect(await screen.findByTestId("credentials-notice")).toHaveTextContent("登录成功");
    vi.useRealTimers();
  });

  it("TRAE 走 callback：不轮询上游，靠凭证列表变化检测完成", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.stubGlobal("open", () => null);
    let credentialCount = 1;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/upstream/start")) {
          return jsonResponse({
            flow: "callback",
            state: "machine:device",
            auth_url: "https://trae.example/login",
            interval: null,
            callback_url: "http://127.0.0.1:8000/authorize",
          });
        }
        return jsonResponse(
          listBody(Array.from({ length: credentialCount }, (_, index) =>
            makeCredential({ id: `c${index}` }))),
        );
      }),
    );

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByTestId("start-login-trae"));
    expect(await screen.findByTestId("cancel-login-trae")).toBeInTheDocument();

    // 浏览器授权完成后服务端落库 → 下一次轮询发现凭证变多
    credentialCount = 2;
    await vi.advanceTimersByTimeAsync(4000);
    expect(await screen.findByTestId("credentials-notice")).toHaveTextContent("登录成功");
    vi.useRealTimers();
  });

  it("取消登录会调用 cancel 接口并移除挂起状态", async () => {
    const fetchSpy = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/upstream/start")) {
        return jsonResponse({ flow: "poll", state: "res-2",
                              auth_url: "https://auth.example/y", interval: 60 });
      }
      if (url.includes("/upstream/cancel")) return jsonResponse({ cancelled: true });
      return jsonResponse(listBody([]));
    });
    vi.stubGlobal("fetch", fetchSpy);
    vi.stubGlobal("open", () => null);

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByTestId("start-login-codebuddy"));
    await screen.findByTestId("cancel-login-codebuddy");
    await userEvent.click(screen.getByTestId("cancel-login-codebuddy"));

    await waitFor(() =>
      expect(fetchSpy.mock.calls.some(([url]) => String(url).includes("/upstream/cancel"))).toBe(true),
    );
    expect(screen.queryByTestId("cancel-login-codebuddy")).not.toBeInTheDocument();
    expect(screen.getByTestId("start-login-codebuddy")).toBeInTheDocument();
  });

  it("非管理员看不到登录入口", async () => {
    mockFetch({ "/api/credentials": listBody([], false) });
    renderPage(<CredentialsPage />, READER);
    await settle();
    expect(screen.queryByTestId("start-login-codebuddy")).not.toBeInTheDocument();
    expect(screen.queryByTestId("start-login-trae")).not.toBeInTheDocument();
  });
});
