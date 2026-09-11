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

  it("探测失败时提示「未探测」而非额度为 0", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/probe")) {
          return jsonResponse({ probed: false, reason: "CredentialQuotaProbeError" });
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
