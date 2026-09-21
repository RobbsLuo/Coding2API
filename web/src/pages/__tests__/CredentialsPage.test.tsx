import { screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CredentialsPage } from "../CredentialsPage";
import { jsonResponse, makeCredential, mockFetch, renderPage, settle, userEvent } from "./helpers";

const ADMIN = { username: "root", is_admin: true } as const;
const READER = { username: "guest", is_admin: false } as const;

function listBody(credentials: unknown[], isAdmin = true) {
  return { credentials, expiry_window_seconds: 129600,
            expiry_secondary_window_seconds: 604800,
            viewer: isAdmin ? "root" : "guest", is_admin: isAdmin };
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
    expect(screen.queryByTestId("actions-cred_1")).not.toBeInTheDocument();
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

  it("窗口内即将到期的积分单独成行；无到期信息的渠道不显示", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ id: "cb", provider: "codebuddy", quota_expiring_credits: 100 }),
        makeCredential({ id: "tr", provider: "trae", quota_expiring_credits: null }),
        makeCredential({ id: "zero", provider: "codebuddy", quota_expiring_credits: 0 }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    const shown = screen.getAllByTestId("quota-expiring");
    expect(shown).toHaveLength(1);
    expect(shown[0]).toHaveTextContent("100 积分将在");
  });

  it("主窗口为空时展示次窗口（7 天）到期积分，措辞与主窗口区分", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({
          id: "week", provider: "codebuddy", health: 50,
          quota_expiring_credits: 0, quota_expiring_credits_secondary: 140,
        }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    expect(screen.queryByTestId("quota-expiring")).not.toBeInTheDocument();
    const secondary = screen.getAllByTestId("quota-expiring-secondary");
    expect(secondary).toHaveLength(1);
    expect(secondary[0]).toHaveTextContent("7.0 天内共 140 积分将过期");
  });

  it("主窗口有数字时不重复渲染次窗口（次窗口是主窗口的超集）", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({
          id: "both", provider: "codebuddy", health: 50,
          quota_expiring_credits: 100, quota_expiring_credits_secondary: 140,
        }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    expect(screen.getAllByTestId("quota-expiring")).toHaveLength(1);
    expect(screen.queryByTestId("quota-expiring-secondary")).not.toBeInTheDocument();
  });

  it("次窗口关闭或无到期信息时不渲染第二行", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ id: "trae2", provider: "trae", health: 50,
                         quota_expiring_credits: null,
                         quota_expiring_credits_secondary: null }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    expect(screen.queryByTestId("quota-expiring")).not.toBeInTheDocument();
    expect(screen.queryByTestId("quota-expiring-secondary")).not.toBeInTheDocument();
  });

  it("额度包明细：单元格只留「套餐 N 个」，悬浮弹出按到期升序的完整明细", async () => {
    const day = Math.floor(Date.now() / 1000) + 86400;
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({
          id: "cb",
          provider: "codebuddy",
          // 故意逆序 + 一个无到期时间：有到期的按先后排，无到期的排最后
          quota_packages: [
            { name: "签到奖励", total: 150, used: 0, end: day + 200_000 },
            { name: "福利积分", total: 2000, used: 0, end: day },
            { name: "每月登录", total: 500, used: 76.564, end: day + 100_000 },
            { name: "无到期包", total: 100, used: 10, end: null },
          ],
        }),
        makeCredential({ id: "tr", provider: "trae", quota_packages: null }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    // 无明细 → 不渲染触发元素
    expect(screen.queryByTestId("packages-toggle-tr")).not.toBeInTheDocument();
    // 表格里只有一行触发元素，不把几十个包撑进单元格
    const toggle = screen.getByTestId("packages-toggle-cb");
    expect(toggle).toHaveTextContent("套餐 4 个");
    expect(screen.queryByTestId("package-cb")).not.toBeInTheDocument();

    await userEvent.hover(toggle);
    const tip = await screen.findByRole("tooltip");
    const items = within(tip).getAllByTestId("package-cb");
    expect(items).toHaveLength(4);
    // 按到期升序：最近到期的「福利积分」排第一，无到期时间的「无到期包」排最后
    expect(items[0]).toHaveTextContent("福利积分");
    expect(items[0]).toHaveTextContent("2,000");
    expect(items[1]).toHaveTextContent("每月登录");
    expect(items[1]).toHaveTextContent("已用 76.56");
    expect(items[3]).toHaveTextContent("无到期包");
    expect(items[3]).toHaveTextContent("— 到期");
    // 明细行很长：弹层要放宽且不折行（默认 max-w-sm + 换行会把每行挤成两三行）
    expect(tip.className).toContain("max-w-[90vw]");
    expect(tip.className).toContain("whitespace-nowrap");
    await userEvent.unhover(toggle);
  });

  it("无额度包明细（未探测或 TRAE）时不显示套餐触发元素", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ id: "empty", provider: "codebuddy", quota_packages: [] }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    expect(screen.queryByTestId("packages-toggle-empty")).not.toBeInTheDocument();
  });

  it("模型级冷却：只写「该模型被避让」，并显示剩余时间与命中次数", async () => {
    const now = Math.floor(Date.now() / 1000);
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({
          id: "cb",
          provider: "codebuddy",
          // 未过期（限流）与已过期（不该上屏）
          model_cooldowns: [
            { model: "glm-5.2", cooling_until: now + 300, hits: 2, reason: "model" },
            { model: "stale", cooling_until: now - 300, hits: 1, reason: "model" },
          ],
        }),
        makeCredential({ id: "plain", provider: "trae", model_cooldowns: [] }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    const block = screen.getByTestId("model-cooldowns-cb");
    expect(block).toHaveTextContent("glm-5.2");
    expect(block).toHaveTextContent("其它模型不受影响");
    expect(block).toHaveTextContent("连续 2 次");
    expect(block).not.toHaveTextContent("stale");
    // 无冷却条目的凭证不渲染该区块
    expect(screen.queryByTestId("model-cooldowns-plain")).not.toBeInTheDocument();
  });

  it("暂停/取消暂停调用 toggle 接口", async () => {
    const fetchSpy = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/toggle")) return jsonResponse({ ok: true });
      return jsonResponse(listBody([makeCredential({ id: "cred_1", enabled: 1 })]));
    });
    vi.stubGlobal("fetch", fetchSpy);

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByTestId("actions-cred_1"));
    await userEvent.click(screen.getByRole("menuitem", { name: "暂停" }));

    await waitFor(() =>
      expect(fetchSpy).toHaveBeenCalledWith(
        "/api/credentials/cred_1/toggle",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("被硬禁用的凭证显示「恢复」并调用 revive 接口", async () => {
    const fetchSpy = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/revive")) return jsonResponse({ ok: true });
      return jsonResponse(listBody([
        makeCredential({ id: "cred_1", disabled: 1, disabled_reason: "session dead" }),
      ]));
    });
    vi.stubGlobal("fetch", fetchSpy);

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByTestId("actions-cred_1"));
    await userEvent.click(screen.getByRole("menuitem", { name: "恢复" }));

    await waitFor(() =>
      expect(fetchSpy).toHaveBeenCalledWith(
        "/api/credentials/cred_1/revive",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("健康凭证不显示「恢复」入口", async () => {
    mockFetch({ "/api/credentials": listBody([makeCredential({ id: "cred_1" })]) });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByTestId("actions-cred_1"));
    expect(screen.queryByRole("menuitem", { name: "恢复" })).not.toBeInTheDocument();
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
    await userEvent.click(screen.getByTestId("actions-cred_1"));
    await userEvent.click(screen.getByRole("menuitem", { name: "指定" }));

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
    await userEvent.click(screen.getByTestId("actions-cred_1"));
    await userEvent.click(screen.getByRole("menuitem", { name: "删除" }));
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
    await userEvent.click(screen.getByTestId("actions-cred_1"));
    await userEvent.click(screen.getByRole("menuitem", { name: "探测" }));

    const notice = await screen.findByTestId("credentials-notice");
    expect(notice).toHaveTextContent("探测失败");
    expect(notice).toHaveTextContent("未探测");
    // 面向用户的是可操作的中文说明，不是枚举或异常类名
    expect(notice).toHaveTextContent("渠道响应格式与预期不符");
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
    await userEvent.click(screen.getByTestId("actions-cred_1"));
    await userEvent.click(screen.getByRole("menuitem", { name: "探测" }));
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
    await userEvent.click(screen.getByTestId("actions-cred_1"));
    await userEvent.click(screen.getByRole("menuitem", { name: "探测" }));

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
    await userEvent.click(screen.getByTestId("actions-cred_1"));
    await userEvent.click(screen.getByRole("menuitem", { name: "签到" }));
    expect(await screen.findByTestId("credentials-notice")).toHaveTextContent("100");
  });

  it("已签到时显示渠道原文，不出现「获得 — 积分」", async () => {
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
    await userEvent.click(screen.getByTestId("actions-cred_1"));
    await userEvent.click(screen.getByRole("menuitem", { name: "签到" }));
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
    await userEvent.click(screen.getByTestId("actions-cred_1"));
    await userEvent.click(screen.getByRole("menuitem", { name: "签到" }));
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
    await userEvent.click(screen.getByTestId("actions-cred_1"));
    await userEvent.click(screen.getByRole("menuitem", { name: "签到" }));
    expect(await screen.findByTestId("credentials-notice")).toHaveTextContent("code=null");
  });

  it("空池提示", async () => {
    mockFetch({ "/api/credentials": listBody([]) });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    expect(screen.getByTestId("no-credentials")).toBeInTheDocument();
  });
});


describe("渠道登录入口", () => {
  it("两个渠道都能发起登录", async () => {
    mockFetch({ "/api/credentials": listBody([]) });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    expect(screen.getByTestId("start-login-codebuddy")).toHaveTextContent("登录 CodeBuddy");
    expect(screen.getByTestId("start-login-trae")).toHaveTextContent("登录 TRAE");
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

describe("CredentialsPage 成长中心", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  const GROWTH_BODY = {
    ok: true,
    report: "领旅行礼物：咖啡馆 带回 10 积分（能量 18，本次 +共 10 积分）",
    credit: 10,
    energy: 18,
    streak_days: null,
    session_dead: false,
    steps: [
      { name: "领旅行礼物", status: "done", detail: "咖啡馆 带回 10 积分", credit: 10 },
      { name: "Buddy 旅行中", status: "idle", detail: "书店，约 1 小时后回", credit: null },
      { name: "开盲盒", status: "skipped", detail: "不可逆动作已关闭", credit: null },
    ],
  };

  // mockFetch 按 URL 子串顺序匹配：growth 的路径也含 "/api/credentials"，
  // 因此必须把 "/growth" 放在前面，否则会被列表路由吞掉。
  function growthRoutes(credential: Record<string, unknown>, growth: unknown) {
    return {
      "/growth": growth,
      "/api/credentials": listBody([credential]),
    };
  }

  it("TRAE 凭证没有成长中心入口（该渠道无此活动）", async () => {
    mockFetch({ "/api/credentials": listBody([makeCredential({ provider: "trae" })]) });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByTestId("actions-cred_1"));
    expect(screen.queryByRole("menuitem", { name: "成长中心" })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "活跃上报" })).not.toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "签到" })).toBeInTheDocument();
  });

  it("CodeBuddy 手动活跃上报：成功/失败各自给出人话反馈", async () => {
    mockFetch({
      "/activity": { ok: false, message: "无法确定账号 userId" },
      "/api/credentials": listBody([makeCredential({ provider: "codebuddy" })]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByTestId("actions-cred_1"));
    await userEvent.click(screen.getByRole("menuitem", { name: "活跃上报" }));
    expect(await screen.findByTestId("credentials-notice")).toHaveTextContent(
      "活跃上报失败：无法确定账号 userId",
    );
  });

  it("CodeBuddy 手动执行成长中心：展示逐条结果并区分「未执行/已关闭」", async () => {
    mockFetch(growthRoutes(
      makeCredential({ provider: "codebuddy", growth_last_result: "上一轮：无可领取项" }),
      GROWTH_BODY,
    ));
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    // 列表列先显示上一轮结果
    expect(screen.getByTestId("growth-cred_1")).toHaveTextContent("上一轮：无可领取项");

    await userEvent.click(screen.getByTestId("actions-cred_1"));
    await userEvent.click(screen.getByRole("menuitem", { name: "成长中心" }));

    const panel = await screen.findByTestId("growth-result");
    expect(panel).toHaveTextContent("领旅行礼物");
    const steps = within(panel).getAllByTestId("growth-step");
    expect(steps).toHaveLength(3);
    expect(steps[0]).toHaveTextContent("已领取领旅行礼物：咖啡馆 带回 10 积分");
    // idle 与 skipped 都不是失败，用中性文案
    expect(steps[1]).toHaveTextContent("未执行Buddy 旅行中");
    expect(steps[2]).toHaveTextContent("已关闭开盲盒");
  });

  it("没有成长中心记录时列表列显示占位符", async () => {
    mockFetch({ "/api/credentials": listBody([makeCredential({ provider: "codebuddy" })]) });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    expect(screen.queryByTestId("growth-cred_1")).not.toBeInTheDocument();
    expect(within(screen.getByTestId("row-cred_1")).getByText("—")).toBeInTheDocument();
  });

  it("长汇报单行截断展示，鼠标悬浮显示完整内容", async () => {
    const longReport =
      "领取任务：「体验「设计创意模式」」失败：prerequisite not met: first_buddy；" +
      "领取任务：「探索优秀灵感」失败：prerequisite not met: first_buddy；接单受阻：" +
      "17 个任务需先完成「领取一只 Buddy（在客户端新建任务并发起对话）」";
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ provider: "codebuddy", growth_last_result: longReport }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    const cell = screen.getByTestId("growth-cred_1");
    // 单元格内是截断的单行样式（truncate），不是把整段挤成多行撑高表格
    const truncated = within(cell).getByText(longReport);
    expect(truncated.className).toContain("truncate");

    // 悬浮后完整内容出现在 tooltip 里（radix 会把内容同时挂到 trigger 的 aria 描述）
    await userEvent.hover(cell);
    const tip = await screen.findByRole("tooltip");
    expect(tip).toHaveTextContent("17 个任务需先完成");
    expect(tip).toHaveTextContent("prerequisite not met: first_buddy");
    // 提示层要能换行显示长文本，否则多行内容会横向溢出
    expect(tip.className).toContain("whitespace-normal");
    await userEvent.unhover(cell);
  });

  it("登录态失效单独提示重新登录（不能与普通失败混同）", async () => {
    mockFetch(growthRoutes(
      makeCredential({ provider: "codebuddy" }),
      { ...GROWTH_BODY, ok: false, session_dead: true,
        report: "登录态已失效，请重新登录",
        steps: [{ name: "查旅行状态", status: "failed",
                  detail: "登录态已失效", credit: null }] },
    ));
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByTestId("actions-cred_1"));
    await userEvent.click(screen.getByRole("menuitem", { name: "成长中心" }));

    const panel = await screen.findByTestId("growth-result");
    expect(panel).toHaveTextContent("需重新登录该渠道");
    expect(within(panel).getByTestId("growth-step")).toHaveTextContent("失败查旅行状态");
  });

  it("成长中心执行失败时展示错误且不显示结果面板", async () => {
    mockFetch(growthRoutes(
      makeCredential({ provider: "codebuddy" }),
      () => jsonResponse({ error: { code: "upstream_unavailable", message: "上游不可用" } }, 502),
    ));
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByTestId("actions-cred_1"));
    await userEvent.click(screen.getByRole("menuitem", { name: "成长中心" }));

    expect(await screen.findByTestId("credentials-error")).toHaveTextContent("上游不可用");
    expect(screen.queryByTestId("growth-result")).not.toBeInTheDocument();
  });
});
