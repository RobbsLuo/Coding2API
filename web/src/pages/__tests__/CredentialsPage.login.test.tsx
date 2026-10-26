import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CredentialsPage } from "../CredentialsPage";
import {
  expandCredentialGroups, jsonResponse, makeCredential, renderPage, settle, userEvent,
  mockCredentialsFetch as mockFetch,
} from "@/test-utils";

const ADMIN = { username: "root", is_admin: true } as const;
const READER = { username: "guest", is_admin: false } as const;

function listBody(credentials: unknown[], isAdmin = true, isOperator = isAdmin) {
  return { credentials, expiry_window_seconds: 129600,
            expiry_secondary_window_seconds: 604800,
            token_expiry_warning_seconds: 3600,
            viewer: isAdmin ? "root" : "guest", is_admin: isAdmin,
            is_operator: isOperator };
}

/** 添加凭证收进对话框：需要操作的用例先点开。 */
async function openAddDialog() {
  await userEvent.click(screen.getByTestId("open-add-dialog"));
  await screen.findByTestId("add-credential-dialog");
}

describe("渠道登录入口", () => {
  it("四个渠道都能发起登录（对话框内）", async () => {
    mockFetch({ "/api/credentials": listBody([]) });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    await openAddDialog();

    expect(screen.getByTestId("start-login-codebuddy")).toHaveTextContent("登录 CodeBuddy");
    expect(screen.getByTestId("start-login-trae")).toHaveTextContent("登录 TRAE");
    expect(screen.getByTestId("start-login-qoder")).toHaveTextContent("登录 Qoder");
    expect(screen.getByTestId("start-login-codearts")).toHaveTextContent("登录 CodeArts");
  });

  it("CodeArts 有签到接口：探测与签到都显示", async () => {
    mockFetch({
      "/api/credentials": listBody([makeCredential({ id: "ca", provider: "codearts" })]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();

    expect(screen.getByTestId("probe-ca")).toBeInTheDocument();
    expect(screen.getByTestId("checkin-ca")).toBeInTheDocument();
    // 有额度接口 → 积分记录入口保留
    expect(screen.getByTestId("credits-ca")).toBeInTheDocument();
  });

  it("Qoder 有签到接口：探测与签到都显示", async () => {
    mockFetch({
      "/api/credentials": listBody([makeCredential({ id: "qd", provider: "qoder" })]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();

    expect(screen.getByTestId("probe-qd")).toBeInTheDocument();
    expect(screen.getByTestId("checkin-qd")).toBeInTheDocument();
  });

  it("CodeBuddy 国际无签到活动：探测保留、签到入口隐藏", async () => {
    // 上游 /billing/meter/checkin-activity-status 回 active:false 且活动
    // 字段全空（渠道侧无此活动，非账号级未开启），后端相应把 checkin* 置
    // None，签到按钮必须同步消失——否则就是一个必然失败的出口。
    mockFetch({
      "/api/credentials": listBody([makeCredential({ id: "ci", provider: "codebuddy_intl" })]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();

    expect(screen.getByTestId("probe-ci")).toBeInTheDocument();
    expect(screen.queryByTestId("checkin-ci")).not.toBeInTheDocument();
  });

  it("CodeBuddy 国际有活跃上报入口、无成长中心入口", async () => {
    // 后端 intl.py 只把 growth 置 None，activity 已恢复继承国内站实现
    // （/v2/report 实测可用），前端判定必须跟能力走：写死 codebuddy 会让
    // 国际版明明能跑却看不到菜单项。
    mockFetch({
      "/api/credentials": listBody([makeCredential({ id: "ci", provider: "codebuddy_intl" })]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    await userEvent.click(screen.getByTestId("actions-ci"));

    expect(screen.getByRole("menuitem", { name: "活跃上报" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "成长中心" })).not.toBeInTheDocument();
  });

  it("国内 CodeBuddy 签到入口仍在（上一条的对照）", async () => {
    mockFetch({
      "/api/credentials": listBody([makeCredential({ id: "cb", provider: "codebuddy" })]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();

    expect(screen.getByTestId("checkin-cb")).toBeInTheDocument();
  });

  it("CodeBuddy 走 poll：打开授权页并轮询渠道", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const opened: string[] = [];
    vi.stubGlobal("open", () => {
      const win = { closed: false, location: { href: "" } };
      // 记录最终被导航到的地址（占位窗口先开空白，再被赋值 auth_url）
      Object.defineProperty(win.location, "href", {
        set: (value: string) => opened.push(value),
        get: () => opened[opened.length - 1] ?? "",
      });
      return win;
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
    await expandCredentialGroups();
    await openAddDialog();
    await userEvent.click(screen.getByTestId("start-login-codebuddy"));

    expect(opened).toEqual(["https://auth.example/x"]);
    expect(await screen.findByTestId("cancel-login-codebuddy")).toBeInTheDocument();

    await vi.advanceTimersByTimeAsync(6000);
    expect(await screen.findByTestId("credentials-notice")).toHaveTextContent("登录成功");
    vi.useRealTimers();
  });

  it("TRAE 走 callback：不轮询渠道，靠凭证列表变化检测完成", async () => {
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
    await expandCredentialGroups();
    await openAddDialog();
    await userEvent.click(screen.getByTestId("start-login-trae"));
    expect(await screen.findByTestId("cancel-login-trae")).toBeInTheDocument();
    // 回调地址一并透出：指错主机时用户能自己看出「跳不回来」的原因
    expect(screen.getByTestId("credentials-notice"))
      .toHaveTextContent("http://127.0.0.1:8000/authorize");

    // 浏览器授权完成后服务端落库 → 下一次轮询发现凭证变多
    credentialCount = 2;
    await vi.advanceTimersByTimeAsync(4000);
    // 浮层沿用同一 testid：授权发起时已有一条提示，这里要等它被最新结果替换
    await waitFor(() =>
      expect(screen.getByTestId("credentials-notice")).toHaveTextContent("登录成功"),
    );
    vi.useRealTimers();
  });

  it("TRAE 回调超时：提示检查回调地址与 PUBLIC_BASE_URL", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.stubGlobal("open", () => null);
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
            callback_url: "https://gw.example/authorize",
          });
        }
        return jsonResponse(listBody([makeCredential({ id: "c0" })]));
      }),
    );

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    await openAddDialog();
    await userEvent.click(screen.getByTestId("start-login-trae"));
    await screen.findByTestId("cancel-login-trae");

    await vi.advanceTimersByTimeAsync(5 * 60 * 1000 + 4000);
    await waitFor(() =>
      expect(screen.getByTestId("credentials-notice")).toHaveTextContent("授权超时"),
    );
    expect(screen.getByTestId("credentials-notice"))
      .toHaveTextContent("PUBLIC_BASE_URL");
    vi.useRealTimers();
  });

  it("TRAE 回调打不开时：粘贴整条回调链接完成登录", async () => {
    // issue #21 后半段：NAS 部署里 TRAE 跳回调直接「网络错误」，
    // refreshToken 只在浏览器地址栏，粘回管理台由 /upstream/complete 兑换。
    vi.stubGlobal("open", () => null);
    let completed = false;
    const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
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
      if (url.includes("/upstream/complete")) {
        completed = String(init?.body).includes("/authorize?refreshToken=RT1");
        return jsonResponse({ status: "success", credential_id: "cred_new" });
      }
      return jsonResponse(listBody([makeCredential({ id: "c0" })]));
    });
    vi.stubGlobal("fetch", fetchSpy);

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    await openAddDialog();
    await userEvent.click(screen.getByTestId("start-login-trae"));

    // 回调轨道同时给出粘贴入口（自动回调仍是主路径）
    const input = await screen.findByTestId("paste-callback-input");
    await userEvent.type(input, "/authorize?refreshToken=RT1&loginTraceID=abc");
    await userEvent.click(screen.getByTestId("paste-callback-submit"));

    expect(await screen.findByTestId("credentials-notice")).toHaveTextContent("登录成功");
    expect(completed).toBe(true);
    expect(screen.queryByTestId("paste-callback")).not.toBeInTheDocument();
  });

  it("CodeArts 走 paste：展示粘贴框，提交回调链接完成登录", async () => {
    const opened: string[] = [];
    vi.stubGlobal("open", () => {
      const win = { closed: false, location: { href: "" } };
      Object.defineProperty(win.location, "href", {
        set: (value: string) => opened.push(value),
        get: () => opened[opened.length - 1] ?? "",
      });
      return win;
    });
    let completed = false;
    const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/upstream/start")) {
        return jsonResponse({
          flow: "paste",
          state: "res-paste",
          auth_url: "https://codearts.example/authorize?x=1",
          interval: null,
        });
      }
      if (url.includes("/upstream/complete")) {
        completed = String(init?.body).includes("http://127.0.0.1:12800/oauth/callback?code=abc");
        return jsonResponse({ status: "success", credential_id: "cred_new" });
      }
      return jsonResponse(listBody([]));
    });
    vi.stubGlobal("fetch", fetchSpy);

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    await openAddDialog();
    await userEvent.click(screen.getByTestId("start-login-codearts"));

    expect(opened).toEqual(["https://codearts.example/authorize?x=1"]);
    // 不自动轮询渠道：改为弹出粘贴框
    expect(fetchSpy.mock.calls.some(([url]) => String(url).includes("/upstream/poll")))
      .toBe(false);
    const input = await screen.findByTestId("paste-callback-input");
    await userEvent.type(input, "http://127.0.0.1:12800/oauth/callback?code=abc&state=ignored");
    await userEvent.click(screen.getByTestId("paste-callback-submit"));

    expect(await screen.findByTestId("credentials-notice")).toHaveTextContent("登录成功");
    expect(completed).toBe(true);
    expect(screen.queryByTestId("paste-callback")).not.toBeInTheDocument();
  });

  it("取消登录会取消当前流程（不再重新 start）并移除挂起状态", async () => {
    const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      void init;
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
    await expandCredentialGroups();
    await openAddDialog();
    await userEvent.click(screen.getByTestId("start-login-codebuddy"));
    await screen.findByTestId("cancel-login-codebuddy");
    await userEvent.click(screen.getByTestId("cancel-login-codebuddy"));

    await waitFor(() =>
      expect(fetchSpy.mock.calls.some(([url]) => String(url).includes("/upstream/cancel"))).toBe(true),
    );
    // cancel 用的是 startLogin 记下的 state；不应为拿 state 再开一次登录
    expect(fetchSpy.mock.calls.filter(([url]) => String(url).includes("/upstream/start")).length)
      .toBe(1);
    const cancelCall = fetchSpy.mock.calls.find(([url]) => String(url).includes("/upstream/cancel"));
    expect(String(cancelCall?.[1]?.body)).toContain("res-2");
    expect(screen.queryByTestId("cancel-login-codebuddy")).not.toBeInTheDocument();
    expect(screen.getByTestId("start-login-codebuddy")).toBeInTheDocument();
  });

  it("页面卸载会停掉登录轮询", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let pollCalls = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/upstream/poll")) {
        pollCalls += 1;
        return jsonResponse({ status: "pending" });
      }
      if (url.includes("/upstream/start")) {
        return jsonResponse({ flow: "poll", state: "s", auth_url: "https://a", interval: 3 });
      }
      return jsonResponse(listBody([]));
    }));
    vi.stubGlobal("open", () => null);

    const page = renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    await openAddDialog();
    await userEvent.click(screen.getByTestId("start-login-codebuddy"));
    await screen.findByTestId("cancel-login-codebuddy");
    await vi.advanceTimersByTimeAsync(7000);
    const callsAtUnmount = pollCalls;

    page.unmount();
    await vi.advanceTimersByTimeAsync(20000);
    expect(pollCalls).toBe(callsAtUnmount);      // 卸载后不再轮询
    vi.useRealTimers();
  });

  it("管理员可一键补回 OpenCode Zen（删除后渠道不丢）", async () => {
    const spy = mockFetch({
      "/api/credentials": listBody([makeCredential({ id: "cb", provider: "codebuddy" })]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    await openAddDialog();

    const button = screen.getByTestId("add-zen");
    expect(button).toHaveTextContent("添加 OpenCode Zen");
    expect(button).not.toBeDisabled();

    await userEvent.click(button);
    await waitFor(() => {
      const posted = spy.mock.calls.find(
        ([url, init]) =>
          String(url).includes("/api/credentials") && init?.method === "POST",
      );
      expect(posted).toBeTruthy();
      expect(String(posted?.[1]?.body)).toContain('"zen"');
    });
    expect(await screen.findByTestId("credentials-notice"))
      .toHaveTextContent("已添加 OpenCode Zen");
  });

  it("已有 OpenCode Zen 凭证时按钮禁用，避免重复占位", async () => {
    mockFetch({
      "/api/credentials": listBody([makeCredential({ id: "z", provider: "zen" })]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    await openAddDialog();

    const button = screen.getByTestId("add-zen");
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent("OpenCode Zen 已添加");
    // 免费层无额度接口、无额度探测：积分记录 / 探测 / 签到入口都不该出现
    expect(screen.queryByTestId("credits-z")).not.toBeInTheDocument();
    expect(screen.queryByTestId("probe-z")).not.toBeInTheDocument();
    expect(screen.queryByTestId("checkin-z")).not.toBeInTheDocument();
  });

  it("管理员可一键补回 Kilo（删除后渠道不丢）", async () => {
    const spy = mockFetch({
      "/api/credentials": listBody([makeCredential({ id: "cb", provider: "codebuddy" })]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    await openAddDialog();

    const button = screen.getByTestId("add-kilo");
    expect(button).toHaveTextContent("添加 Kilo");
    expect(button).not.toBeDisabled();

    await userEvent.click(button);
    await waitFor(() => {
      const posted = spy.mock.calls.find(
        ([url, init]) =>
          String(url).includes("/api/credentials") && init?.method === "POST",
      );
      expect(posted).toBeTruthy();
      expect(String(posted?.[1]?.body)).toContain('"kilo"');
    });
    expect(await screen.findByTestId("credentials-notice"))
      .toHaveTextContent("已添加 Kilo");
  });

  it("已有 Kilo 凭证时按钮禁用，避免重复占位", async () => {
    mockFetch({
      "/api/credentials": listBody([makeCredential({ id: "k", provider: "kilo" })]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    await openAddDialog();

    const button = screen.getByTestId("add-kilo");
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent("Kilo 已添加");
    expect(screen.queryByTestId("credits-k")).not.toBeInTheDocument();
    expect(screen.queryByTestId("probe-k")).not.toBeInTheDocument();
    expect(screen.queryByTestId("checkin-k")).not.toBeInTheDocument();
  });

  it("非管理员看不到登录入口", async () => {
    mockFetch({ "/api/credentials": listBody([], false) });
    renderPage(<CredentialsPage />, READER);
    await settle();
    await expandCredentialGroups();
    expect(screen.queryByTestId("start-login-codebuddy")).not.toBeInTheDocument();
    expect(screen.queryByTestId("start-login-trae")).not.toBeInTheDocument();
    expect(screen.queryByTestId("add-zen")).not.toBeInTheDocument();
    expect(screen.queryByTestId("add-kilo")).not.toBeInTheDocument();
  });
});

