import { screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CredentialsPage } from "../CredentialsPage";
import {
  expandCredentialGroups, jsonResponse, makeCredential, mockFetch, renderPage, settle, userEvent,
} from "@/test-utils";

const ADMIN = { username: "root", is_admin: true } as const;
const READER = { username: "guest", is_admin: false } as const;
// 运维角色：能写凭证但非管理员（is_operator 由后端按 role 派生）
const OPERATOR = { username: "op", is_admin: false, role: "operator" } as const;

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

describe("CredentialsPage", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("渲染凭证列表与四态健康度（免费层 null 归「无探测」）", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ id: "a", health: 62 }),
        makeCredential({ id: "b", nickname: "未探测号", health: null, provider: "trae" }),
        makeCredential({ id: "d", nickname: "免费层号", health: null, provider: "zen" }),
        makeCredential({ id: "c", nickname: "耗尽号", health: -1 }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();

    const table = screen.getByTestId("credentials-table");
    // 62% 是行内健康度徽章；组头的额度占比环也可能显示同样的数字，故限定到该行
    expect(within(screen.getByTestId("row-a")).getByText("62%")).toBeInTheDocument();
    expect(within(table).getByText("未探测")).toBeInTheDocument();
    expect(within(table).getByText("无探测")).toBeInTheDocument();
    expect(within(table).getByText("已耗尽")).toBeInTheDocument();
  });

  it("非管理员隐藏所有写操作并显示只读横幅", async () => {
    mockFetch({ "/api/credentials": listBody([makeCredential()], false) });
    renderPage(<CredentialsPage />, READER);
    await settle();
    await expandCredentialGroups();

    expect(screen.getByTestId("readonly-banner")).toHaveTextContent("只读模式");
    expect(screen.queryByTestId("actions-cred_1")).not.toBeInTheDocument();
    // 积分记录也要藏：它只藏入口，后端仍会 403，但只读视图不该给出可点假象
    expect(screen.queryByTestId("credits-cred_1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("import-submit")).not.toBeInTheDocument();
    expect(screen.queryByTestId("start-login")).not.toBeInTheDocument();
  });

  it("运维角色（非管理员）可见写操作，与后端 require_operator 同口径", async () => {
    mockFetch({ "/api/credentials": listBody([makeCredential()], false, true) });
    renderPage(<CredentialsPage />, OPERATOR);
    await settle();
    await expandCredentialGroups();

    expect(screen.queryByTestId("readonly-banner")).not.toBeInTheDocument();
    expect(screen.getByTestId("actions-cred_1")).toBeInTheDocument();
    expect(screen.getByTestId("credits-cred_1")).toBeInTheDocument();
    // 导入入口收进对话框：按钮可见即代表可写（对话框内才有表单）
    const addButton = screen.getByTestId("open-add-dialog");
    expect(addButton).toBeInTheDocument();
    // 按钮挂在「凭证池」面板的标题栏右侧，不在页头
    const panel = screen.getByText("凭证池").closest("[data-slot='card']");
    expect(panel).toContainElement(addButton);
  });

  it("只读用户看不到凭证池标题栏的添加按钮", async () => {
    mockFetch({ "/api/credentials": listBody([makeCredential()], false, false) });
    renderPage(<CredentialsPage />, READER);
    await settle();
    await expandCredentialGroups();
    expect(screen.queryByTestId("open-add-dialog")).not.toBeInTheDocument();
  });

  it("冷却中的凭证显示剩余时间", async () => {
    const future = Math.floor(Date.now() / 1000) + 7200;
    mockFetch({
      "/api/credentials": listBody([makeCredential({ cooling_until: future })]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();

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
    await expandCredentialGroups();

    const shown = screen.getAllByTestId("quota-expiring");
    expect(shown).toHaveLength(1);
    expect(shown[0]).toHaveTextContent("100 积分将在");
  });

  it("CodeArts 单组的健康度与额度列同源，不出现「积分近满却额度耗尽」", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({
          id: "ca", provider: "codearts",
          // 单一账本（积分）：额度近满 + 健康度 100%，两者一致
          health: 100, quota_remaining: 13498.72, quota_total: 13500,
        }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();

    const row = screen.getByTestId("row-ca");
    expect(within(row).getByText("100%")).toBeInTheDocument();
    expect(within(row).getByText("13,498.72 / 13,500")).toBeInTheDocument();
    expect(within(row).queryByText("已耗尽")).not.toBeInTheDocument();
    expect(within(row).queryByText("额度耗尽")).not.toBeInTheDocument();
    // 不再有第二份（福利 / 内置）重复行
    expect(screen.queryByTestId("row-ca-benefit")).not.toBeInTheDocument();
  });

  it("CodeArts 单组按通用字段展示本组到期积分（主 / 次窗口）", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({
          id: "ca", provider: "codearts", health: 100,
          quota_expiring_credits: 999.5,
          quota_expiring_credits_secondary: 1500,
        }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();

    // 一个 CodeArts 组：主窗口读 quota_expiring_credits、次窗口读 secondary
    expect(screen.getByTestId("group-codearts-expiring")).toHaveTextContent("999.5");
    expect(screen.getByTestId("group-codearts-expiring-secondary")).toHaveTextContent("1,500");
    expect(screen.queryByTestId("group-codearts-benefit-expiring")).not.toBeInTheDocument();
  });

  it("CodeArts 无主窗口到期积分时只隐藏主窗口那一块，次窗口仍可见", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({
          id: "ca", provider: "codearts", health: 100,
          quota_expiring_credits: null, quota_expiring_credits_secondary: 40,
        }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();

    expect(screen.queryByTestId("group-codearts-expiring")).not.toBeInTheDocument();
    expect(screen.getByTestId("group-codearts-expiring-secondary")).toHaveTextContent("40");
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
    await expandCredentialGroups();

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
    await expandCredentialGroups();

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
    await expandCredentialGroups();

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
    await expandCredentialGroups();

    // 无明细 → 不渲染触发元素
    expect(screen.queryByTestId("packages-toggle-tr")).not.toBeInTheDocument();
    // 表格里只有一行触发元素，不把几十个包撑进单元格
    const toggle = screen.getByTestId("packages-toggle-cb");
    expect(toggle).toHaveTextContent("套餐 4 个");
    expect(screen.queryByTestId("package-cb")).not.toBeInTheDocument();
    // 「套餐 N 个」与首行「剩余 / 总量」同处一行的 flex 容器；
    // 数字那侧多包了一层 inline-flex（对齐积分记录图标按钮），所以要上溯一层
    const row = screen.getByTestId("row-cb");
    const quotaSpan = within(row).getByText("62 / 100");
    expect(quotaSpan.parentElement?.parentElement).toBe(toggle.parentElement);
    expect(toggle.parentElement).toHaveClass("flex-wrap");

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
    // 几十个包必然超出视口：高度要按 radix 给的可用高度封顶再内滚，
    // 只写 70vh 时贴边弹出会被顶出视口（上半截看不见）
    const body = screen.getByTestId("packages-cb");
    expect(body.className).toContain("--radix-tooltip-content-available-height");
    expect(body.className).toContain("overflow-y-auto");
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
    await expandCredentialGroups();

    expect(screen.queryByTestId("packages-toggle-empty")).not.toBeInTheDocument();
  });

  it("CodeArts 单组头条读通用额度字段，口径说明是积分余额", async () => {
    mockFetch({
      "/api/credentials": listBody([
        // CodeArts：只有一本账（积分），额度直接读通用字段
        makeCredential({
          id: "ca", provider: "codearts",
          quota_remaining: 13498.72, quota_total: 13500,
        }),
        // TRAE：没有积分账，头条就是它自己的账户余额
        makeCredential({ id: "tr", provider: "trae" }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();

    const codearts = screen.getByTestId("row-ca");
    expect(within(codearts).getByText("13,498.72 / 13,500")).toBeInTheDocument();
    // 口径说明挂在额度数字的 title 上（CodeArts＝积分余额）
    expect(within(codearts).getByText("13,498.72 / 13,500")).toHaveAttribute(
      "title", expect.stringContaining("积分余额"));
    // TRAE 头条是它自己的余额
    expect(within(screen.getByTestId("row-tr")).getByText("62 / 100")).toBeInTheDocument();
    // 只有一组 / 一行
    expect(screen.queryByTestId("row-ca-benefit")).not.toBeInTheDocument();
  });

  it("CodeArts 额度未探测到（两字段皆 null）时头条显示 —，不冒充其它数", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ id: "ca", provider: "codearts",
                         quota_remaining: null, quota_total: null }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();

    // 未探测到就是「— / —」，绝不回落到任何占位数
    const codearts = screen.getByTestId("row-ca");
    expect(within(codearts).getByText("— / —")).toBeInTheDocument();
  });

  it("积分流水抽屉按组读对应账本（内置＝积分账，其余＝每日池）", async () => {
    const now = Math.floor(Date.now() / 1000);
    mockFetch({
      "/credit-events": { events: [
        { id: "cre_1", credential_id: "ca", ts: now, window_start: null,
          before: null, after: 100, delta: null, source: "sync", ledger: "credits" },
      ] },
      "/api/credentials": listBody([
        makeCredential({ id: "ca", provider: "codearts" }),
        makeCredential({ id: "tr", provider: "trae" }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();

    // 内置组讲积分账：抽屉标题与说明都按积分账写，且请求明确带上 ledger=credits，
    // 否则会读到福利组的每日池流水（两本账量级差一个数量级，混读会误导）。
    await userEvent.click(screen.getByTestId("credits-ca"));
    const internal = await screen.findByTestId("credit-drawer-ca");
    expect(internal).toHaveTextContent("积分流水");
    expect(internal).toHaveTextContent(
      "积分账（内置模型计费）两次额度探测之间的净变化（非动作归因）");
    expect(fetch).toHaveBeenLastCalledWith(
      expect.stringContaining("ledger=credits"), expect.anything());
    await userEvent.click(screen.getByTestId("credit-drawer-close"));

    // 只有一本账的渠道读每日池：标题是「额度流水」，不出现积分账那句
    await userEvent.click(screen.getByTestId("credits-tr"));
    const single = await screen.findByTestId("credit-drawer-tr");
    expect(single).toHaveTextContent("额度流水");
    expect(single).toHaveTextContent("每日额度池两次额度探测之间的净变化（非动作归因）");
    expect(single).not.toHaveTextContent("积分账");
    expect(fetch).toHaveBeenLastCalledWith(
      expect.stringContaining("ledger=pool"), expect.anything());
  });

  it("额度列不再显示「探测于」时间戳", async () => {
    mockFetch({ "/api/credentials": listBody([makeCredential({ id: "cb" })]) });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();

    const row = screen.getByTestId("row-cb");
    expect(within(row).queryByText(/探测于/)).not.toBeInTheDocument();
  });

  it("模型级冷却：表格里只留警告图标，hover 才展开明细", async () => {
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
    await expandCredentialGroups();

    // 明细不常驻表格：不 hover 时单元格里没有冷却文案，只有一个警告图标
    expect(screen.queryByTestId("model-cooldowns-cb")).not.toBeInTheDocument();
    const icon = screen.getByTestId("model-cooldown-icon-cb");
    const row = screen.getByTestId("row-cb");
    expect(within(row).queryByText(/模型 glm-5\.2/)).not.toBeInTheDocument();

    await userEvent.hover(icon);
    const block = await screen.findByTestId("model-cooldowns-cb");
    expect(block).toHaveTextContent("glm-5.2");
    expect(block).toHaveTextContent("其它模型不受影响");
    expect(block).toHaveTextContent("连续 2 次");
    expect(block).not.toHaveTextContent("stale");
    await userEvent.unhover(icon);

    // 无冷却条目的凭证不渲染图标
    expect(screen.queryByTestId("model-cooldown-icon-plain")).not.toBeInTheDocument();
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
    await expandCredentialGroups();
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
    await expandCredentialGroups();
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
    await expandCredentialGroups();
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
    await expandCredentialGroups();
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
    await expandCredentialGroups();
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
    await expandCredentialGroups();
    await openAddDialog();

    await userEvent.click(screen.getByTestId("add-credential-tab-import"));
    await userEvent.type(screen.getByTestId("import-payload"), "not-json-at-all");
    await userEvent.click(screen.getByTestId("import-submit"));

    expect(screen.getByText(/必须是合法 JSON/)).toBeInTheDocument();
    // 只有列表请求（侧栏角标是 Layout 发的另一路，不算导入请求）
    expect(fetchSpy.mock.calls.filter((call) => call[1]?.method === "POST")).toHaveLength(0);
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
    await expandCredentialGroups();
    await openAddDialog();
    await userEvent.click(screen.getByTestId("add-credential-tab-import"));
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
    await expandCredentialGroups();
    await userEvent.click(screen.getByTestId("probe-cred_1"));

    const notice = await screen.findByTestId("credentials-notice");
    expect(notice).toHaveTextContent("探测失败");
    expect(notice).toHaveTextContent("未探测");
    // 面向用户的是可操作的中文说明，不是枚举或异常类名
    expect(notice).toHaveTextContent("渠道响应格式与预期不符");
    expect(notice).not.toHaveTextContent("upstream_response_invalid");
    // 上游原文不再回显给前端（可能含凭证/上游端点）
    expect(screen.queryByTestId("probe-detail")).toBeNull();
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
    await expandCredentialGroups();
    await userEvent.click(screen.getByTestId("probe-cred_1"));
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
    await expandCredentialGroups();
    await userEvent.click(screen.getByTestId("probe-cred_1"));

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
    await expandCredentialGroups();
    await userEvent.click(screen.getByTestId("checkin-cred_1"));
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
    await expandCredentialGroups();
    await userEvent.click(screen.getByTestId("checkin-cred_1"));
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
    await expandCredentialGroups();
    await userEvent.click(screen.getByTestId("checkin-cred_1"));
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
    await expandCredentialGroups();
    await userEvent.click(screen.getByTestId("checkin-cred_1"));
    expect(await screen.findByTestId("credentials-notice")).toHaveTextContent("code=null");
  });

  it("空池提示", async () => {
    mockFetch({ "/api/credentials": listBody([]) });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    expect(screen.getByTestId("no-credentials")).toBeInTheDocument();
    // 空池没有可筛的东西，工具条不出现
    expect(screen.queryByTestId("credentials-toolbar")).not.toBeInTheDocument();
  });

  it("CodeArts 只渲染一个可写组：账号只出现一次，操作齐全", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({
          id: "ca1", provider: "codearts",
          quota_remaining: 13498.72, quota_total: 13500,
        }),
        makeCredential({ id: "tr1", provider: "trae" }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();

    // 切到全站渠道顺序，组顺序才确定（默认按额度排，CodeArts 13,498 > TRAE 62）
    await userEvent.selectOptions(screen.getByTestId("credential-group-sort"), "provider");

    // 组头：TRAE 在前、CodeArts 在后，各一行（不再拆内置 / 福利两组）。
    // 组 key 含连字符，故显式排掉同前缀的指标块（-state- / -quota-pct / -expiring）。
    const headers = screen
      .getAllByTestId(/^group-[a-z-]+$/)
      .filter((node) => !/-state-|-quota-pct|-expiring|-select-|-toggle-/.test(
        node.getAttribute("data-testid") ?? ""));
    expect(headers.map((node) => node.getAttribute("data-testid"))).toEqual([
      "group-trae",
      "group-codearts",
    ]);
    expect(screen.getByTestId("group-codearts")).toHaveTextContent("CodeArts");
    expect(screen.getByTestId("group-codearts")).toHaveTextContent("1 个账号");

    // 唯一的 CodeArts 行：全操作 + 勾选 + 流水入口 + 额度头条都在（不再有只读组）。
    const row = screen.getByTestId("row-ca1");
    expect(within(row).getByText("13,498.72 / 13,500")).toBeInTheDocument();
    expect(screen.getByTestId("select-ca1")).toBeInTheDocument();
    expect(screen.getByTestId("actions-ca1")).toBeInTheDocument();
    expect(screen.getByTestId("credits-ca1")).toBeInTheDocument();
    expect(screen.getByTestId("group-select-codearts")).toBeInTheDocument();
    // 不再有 -benefit / -internal 那份重复行
    expect(screen.queryByTestId("row-ca1-benefit")).not.toBeInTheDocument();
    expect(screen.queryByTestId("row-ca1-internal")).not.toBeInTheDocument();

    // 列数恒定：列头 / 数据行 / 组头 colspan 都是 7，否则 auto 布局整列错位
    expect(screen.getByTestId("column-header-codearts").children).toHaveLength(7);
    expect(screen.getByTestId("group-codearts").querySelector("td"))
      .toHaveAttribute("colspan", "7");
    expect(screen.getByTestId("row-ca1").children).toHaveLength(7);
    // 可写组的操作列有列名
    expect([...screen.getByTestId("column-header-codearts").children][6])
      .toHaveTextContent("操作");
  });

  it("只读用户下 CodeArts 仍是单组，列数与其它组一致", async () => {
    mockFetch({
      "/api/credentials": listBody(
        [makeCredential({ id: "ca1", provider: "codearts" })],
        false,
        false,
      ),
    });
    renderPage(<CredentialsPage />, READER);
    await settle();
    await expandCredentialGroups();

    // viewer 本来就没有选择 / 操作列，单组是 5 格（凭证 / 状态 / 额度 / token / 成长占位）
    expect(screen.getByTestId("column-header-codearts").children).toHaveLength(5);
    expect(screen.getByTestId("group-codearts").querySelector("td"))
      .toHaveAttribute("colspan", "5");
    expect(screen.getByTestId("row-ca1").children).toHaveLength(5);
    expect(screen.queryByTestId("row-ca1-benefit")).not.toBeInTheDocument();
  });

  it("按渠道分组：组头显示渠道名与数量，行内不再复读渠道名", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ id: "cb1", provider: "codebuddy", nickname: "主号" }),
        makeCredential({ id: "cb2", provider: "codebuddy", nickname: "小号" }),
        makeCredential({ id: "tr1", provider: "trae", nickname: "TRAE 号" }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();

    // 组头按全站渠道顺序：CodeBuddy 在 TRAE 前
    // （testid 精确到 group-<渠道>：group-<渠道>-state-<态> / -expiring 也是同前缀）
    const groupHeaders = screen.getAllByTestId(/^group-[a-z_]+$/);
    expect(groupHeaders.map((node) => node.getAttribute("data-testid"))).toEqual([
      "group-codebuddy",
      "group-trae",
    ]);
    expect(screen.getByTestId("group-codebuddy")).toHaveTextContent("CodeBuddy");
    expect(screen.getByTestId("group-codebuddy")).toHaveTextContent("2 个账号");
    expect(screen.getByTestId("group-codebuddy")).toHaveTextContent("可用 2");
    // 组头带额度合计（两个默认号各 62 / 100；标签行还带占比环的精确百分比）
    expect(screen.getByTestId("group-codebuddy")).toHaveTextContent("额度合计62%124 / 200");
    // 渠道名不再逐行重复：数据行里找不到渠道名文本（ProviderIcon 的 SVG
    // <title> 不算可见文本，用 selector 限定到元素文本节点）
    for (const id of ["cb1", "cb2", "tr1"]) {
      expect(
        within(screen.getByTestId(`row-${id}`)).queryByText("CodeBuddy", {
          selector: "span, div, td",
        }),
      ).not.toBeInTheDocument();
    }
  });

  it("组头显示该渠道当前能提供的模型数与最低倍率，hover 看逐条倍率", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ id: "cb1", provider: "codebuddy" }),
        makeCredential({ id: "tr1", provider: "trae" }),
      ]),
      "/api/playground/models": {
        object: "list",
        data: [
          // 多渠道条目：两个渠道的组头都要算上它，倍率各按自己的渠道取
          {
            id: "glm-5.2", name: "GLM 5.2", providers: ["codebuddy", "trae"],
            by_provider: { codebuddy: { credit_rate: 0.29 }, trae: { credit_rate: 0.5 } },
          },
          { id: "kimi-k3", name: "Kimi K3", providers: ["codebuddy"], credit_rate: 0.8 },
          // 免费额度：0 必须显示成「免」，不能是 x0
          { id: "free-model", name: "Free Model", providers: ["trae"], credit_rate: 0 },
        ],
      },
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    // 折叠态就可见：数量 + 最低倍率（先回答「这渠道贵不贵」）
    const cbCount = screen.getByTestId("group-codebuddy-model-count");
    expect(cbCount).toHaveTextContent("2 个");
    expect(cbCount).toHaveTextContent("最低 x0.29");
    const trCount = screen.getByTestId("group-trae-model-count");
    expect(trCount).toHaveTextContent("2 个");
    expect(trCount).toHaveTextContent("最低 免");

    // 逐条清单不铺在组头（几十条会把这一行撑爆），hover 才展开
    expect(screen.queryByTestId("group-codebuddy-models")).not.toBeInTheDocument();
    await userEvent.hover(cbCount);
    const tip = await screen.findByTestId("group-codebuddy-models");
    // 头部：渠道身份 + 条数（条数单独成胶囊，不混进渠道名一句话里）
    expect(tip).toHaveTextContent("CodeBuddy");
    expect(tip).toHaveTextContent("2 个模型");
    const rows = within(tip).getAllByTestId("group-codebuddy-model");
    expect(rows.map((row) => row.textContent)).toEqual([
      "GLM 5.2x0.29",
      "Kimi K3x0.8",
    ]);
    // 多行清单走 panel 表面（卡片底 + 描边 + 阴影），不是反色气泡：反色底放大段
    // 文字会糊成一块，且气泡的箭头会盖住描边留一道缺口
    expect(tip.closest("[data-slot='tooltip-content']"))
      .toHaveAttribute("data-surface", "panel");
    await userEvent.unhover(cbCount);

    // 同一模型在 TRAE 组里显示的是 TRAE 的倍率（0.5，不是 CodeBuddy 的 0.29）
    await userEvent.hover(trCount);
    const traeTip = await screen.findByTestId("group-trae-models");
    const traeRows = within(traeTip).getAllByTestId("group-trae-model");
    expect(traeRows.map((row) => row.textContent)).toEqual([
      "Free Model免",
      "GLM 5.2x0.5",
    ]);
    await userEvent.unhover(trCount);
  });

  it("模型都未给倍率时只显示数量，不造一个「最低」", async () => {
    mockFetch({
      "/api/credentials": listBody([makeCredential({ id: "cb1", provider: "codebuddy" })]),
      "/api/playground/models": {
        object: "list",
        data: [{ id: "glm-5.2", name: "GLM 5.2", providers: ["codebuddy"] }],
      },
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    const count = screen.getByTestId("group-codebuddy-model-count");
    expect(count).toHaveTextContent("1 个");
    expect(count).not.toHaveTextContent("最低");
    // 逐条倍率未知时显示 —（不当 0 读成免费）
    await userEvent.hover(count);
    const tip = await screen.findByTestId("group-codebuddy-models");
    expect(within(tip).getByTestId("group-codebuddy-model")).toHaveTextContent("GLM 5.2—");
  });

  it("模型列表为空时不显示「模型 0 个」（0 会被读成这渠道没有模型）", async () => {
    mockFetch({ "/api/credentials": listBody([makeCredential({ id: "cb1", provider: "codebuddy" })]) });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    expect(screen.queryByTestId("group-codebuddy-model-count")).not.toBeInTheDocument();
  });

  it("组头聚合状态分布与 1.5 / 7 天内将过期的积分，折叠时也读得到", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({
          id: "cb1", provider: "codebuddy", quota_expiring_credits: 100,
          quota_expiring_credits_secondary: 260,
        }),
        makeCredential({
          id: "cb2", provider: "codebuddy", cooling_until: 9_999_999_999,
          quota_expiring_credits: 30, quota_expiring_credits_secondary: 40,
        }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    // 默认折叠：组头就把状态分布和到期积分讲清楚
    const header = screen.getByTestId("group-codebuddy");
    expect(header).toHaveTextContent("可用");
    expect(header).toHaveTextContent("冷却中");
    expect(screen.getByTestId("group-codebuddy-state-ready")).toHaveTextContent("1");
    expect(screen.getByTestId("group-codebuddy-state-cooling")).toHaveTextContent("1");
    // 主窗口（1.5 天）合计 130，次窗口（7 天）合计 300（多出 170，值得单独一块）
    expect(screen.getByTestId("group-codebuddy-expiring")).toHaveTextContent("130");
    expect(screen.getByTestId("group-codebuddy-expiring-secondary")).toHaveTextContent("300");
    // 形象化：每个指标都配了图形（状态图标 + 额度 / 到期图标），不只靠文字与颜色
    expect(header.querySelectorAll("svg").length).toBeGreaterThan(4);
  });

  it("次窗口不比主窗口多时不重复展示（两窗口数字恒等）", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({
          id: "cb1", provider: "codebuddy",
          quota_expiring_credits: 3_000,
          quota_expiring_credits_secondary: 3_000,
        }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    // 两窗口数字相同 = 没有超出主窗口的部分，只列主窗口那一块
    expect(screen.getByTestId("group-codebuddy-expiring")).toHaveTextContent("3,000");
    expect(screen.queryByTestId("group-codebuddy-expiring-secondary"))
      .not.toBeInTheDocument();
  });

  it("CodeArts 单组的行内到期积分读通用字段（与组头同源）", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({
          id: "ca1", provider: "codearts",
          quota_expiring_credits: 3_000,
          quota_expiring_credits_secondary: 9_000,
        }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();

    // 行内到期积分与组头同源，都读通用的主窗口字段；
    // 次窗口是主窗口的超集，主窗口已有数字时行内不重复渲染（见 Row）
    expect(screen.getByTestId("quota-expiring")).toHaveTextContent("3,000 积分将在");
    expect(screen.queryByTestId("quota-expiring-secondary")).not.toBeInTheDocument();
    expect(screen.getByTestId("group-codearts-expiring")).toHaveTextContent("3,000");
    expect(screen.getByTestId("group-codearts-expiring-secondary")).toHaveTextContent("9,000");
  });

  it("组头额度在免费层显示 —（无额度接口，不当成 0）", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ id: "z", provider: "zen", quota_remaining: null, quota_total: null }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    expect(screen.getByTestId("group-zen")).toHaveTextContent("额度合计—");
  });

  it("组默认折叠，点组头展开/收起该渠道的行", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ id: "cb1", provider: "codebuddy" }),
        makeCredential({ id: "tr1", provider: "trae" }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    // 默认全折叠：组头在、行不在，也没有列头（列头跟着展开区走）
    const toggle = screen.getByTestId("group-toggle-codebuddy");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("row-cb1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("row-tr1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("column-header-codebuddy")).not.toBeInTheDocument();

    // 展开只影响该组：别的渠道仍然折叠；列头只在该组出现
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("row-cb1")).toBeInTheDocument();
    expect(screen.getByTestId("column-header-codebuddy")).toBeInTheDocument();
    expect(screen.queryByTestId("row-tr1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("column-header-trae")).not.toBeInTheDocument();

    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("row-cb1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("column-header-codebuddy")).not.toBeInTheDocument();
  });

  it("组头指标块等宽：各渠道的「状态 / 额度 / 到期」成列对齐", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ id: "cb1", provider: "codebuddy" }),
        // 更长的渠道名 + 更多状态：内容宽度不同，也不该把后面的指标推来推去
        makeCredential({ id: "cb2", provider: "codebuddy", cooling_until: 9_999_999_999 }),
        makeCredential({ id: "z", provider: "zen", quota_remaining: null, quota_total: null }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    // 身份块与状态 / 额度 / 到期块都用固定宽度（w-* shrink-0），不随内容伸缩——
    // jsdom 无排版，用类名锁住这个契约；真实对齐在浏览器里验证。
    const fixedWidths = (provider: string) => {
      const toggle = screen.getByTestId(`group-toggle-${provider}`);
      const blocks = [
        ...toggle.children,
        ...([...toggle.children] as HTMLElement[]).flatMap((child) => [...child.children]),
      ] as HTMLElement[];
      return blocks
        .map((block) => block.getAttribute("class") ?? "")
        .flatMap((cls) => {
          const match = cls.match(/(?:^|\s)(w-\d+)(?:\s|$)/);
          return match ? [{ cls, width: match[1] }] : [];
        });
    };
    for (const provider of ["codebuddy", "zen"]) {
      const fixed = fixedWidths(provider);
      // 身份 + 状态 + 额度（zen 无到期块）
      expect(fixed.length).toBeGreaterThanOrEqual(3);
      for (const block of fixed) {
        expect(block.cls).toContain("shrink-0");
      }
    }

    // 两个渠道的指标块宽度完全一致（同一套类名），故各组起点对齐
    expect(fixedWidths("codebuddy").map((b) => b.width))
      .toEqual(fixedWidths("zen").map((b) => b.width));
  });

  it("组排序：默认「额度从高到低」，可切回渠道顺序，免费层垫底", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ id: "cb1", provider: "codebuddy", quota_remaining: 50, quota_total: 100 }),
        makeCredential({ id: "tr1", provider: "trae", quota_remaining: 900, quota_total: 1000 }),
        makeCredential({ id: "z1", provider: "zen", quota_remaining: null, quota_total: null }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    const groupOrder = () =>
      screen
        .getAllByTestId(/^group-[a-z_]+$/)
        .map((node) => node.getAttribute("data-testid"));

    // 默认：额度从高到低 —— TRAE 900 > CodeBuddy 50 > Zen（无额度接口，垫底）
    expect(groupOrder()).toEqual(["group-trae", "group-codebuddy", "group-zen"]);

    const select = screen.getByTestId("credential-group-sort");
    expect(select).toHaveValue("quota");

    // 切到全站渠道顺序（CodeBuddy → TRAE → Zen）
    await userEvent.selectOptions(select, "provider");
    expect(groupOrder()).toEqual(["group-codebuddy", "group-trae", "group-zen"]);

    // 切回额度排序
    await userEvent.selectOptions(select, "quota");
    expect(groupOrder()).toEqual(["group-trae", "group-codebuddy", "group-zen"]);
  });

  it("组头额度占比用环：环存在且标签行给出精确百分比，免费层不出环", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ id: "cb1", provider: "codebuddy", quota_remaining: 75, quota_total: 100 }),
        makeCredential({ id: "z1", provider: "zen", quota_remaining: null, quota_total: null }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    // 75 / 100 = 75%：环是纯图形仪表，百分比文字在「额度合计」标签行
    const ring = screen.getByTestId("group-codebuddy-quota-pct");
    expect(ring.querySelector("svg")).toBeInTheDocument();
    expect(screen.getByTestId("group-codebuddy")).toHaveTextContent("额度合计75%");
    // 免费层无额度接口：不冒充 0%，不渲染环
    expect(screen.queryByTestId("group-zen-quota-pct")).not.toBeInTheDocument();
    expect(screen.getByTestId("group-zen")).toHaveTextContent("额度合计—");
  });

  it("占比环覆盖 0% / 100% 两个端点（不因 round 端帽留下一个圆点）", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ id: "full", provider: "qoder", quota_remaining: 100, quota_total: 100 }),
        makeCredential({
          id: "empty", provider: "codearts",
          quota_remaining: 0, quota_total: 100,
        }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    // 满额：整圈实线，无 dasharray（零长段 + round 端帽会多出一个点）
    const full = screen.getByTestId("group-qoder-quota-pct").querySelectorAll("circle");
    expect(full).toHaveLength(2);
    expect(full[1].getAttribute("stroke-dasharray")).toBeNull();

    // 归零：只剩淡色轨道，不画进度弧
    const empty = screen.getByTestId("group-codearts-quota-pct").querySelectorAll("circle");
    expect(empty).toHaveLength(1);

    expect(screen.getByTestId("group-qoder")).toHaveTextContent("额度合计100%");
    // CodeArts 组讲积分账，标签是「积分合计」
    expect(screen.getByTestId("group-codearts")).toHaveTextContent("积分合计0%");
  });

  it("列头迁进展开区：每个展开的渠道各有一份列头，都能触发同一套排序", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ id: "cb1", provider: "codebuddy" }),
        makeCredential({ id: "tr1", provider: "trae" }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();

    // 两个组各渲染一份列头，列名齐全（不再有全局表头）
    for (const provider of ["codebuddy", "trae"]) {
      const header = screen.getByTestId(`column-header-${provider}`);
      expect(within(header).getByText("凭证")).toBeInTheDocument();
      expect(within(header).getByText("token 剩余")).toBeInTheDocument();
    }
    // 成长中心列只在支持的渠道出现：CodeBuddy 有，TRAE 是空占位格（列数恒定，见 Row / ColumnHeaderRow）
    expect(
      within(screen.getByTestId("column-header-codebuddy")).getByText("成长中心"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId("column-header-trae")).queryByText("成长中心"),
    ).not.toBeInTheDocument();
    // 折叠后列头随之消失
    await userEvent.click(screen.getByTestId("group-toggle-trae"));
    expect(screen.queryByTestId("column-header-trae")).not.toBeInTheDocument();
    expect(screen.getByTestId("column-header-codebuddy")).toBeInTheDocument();

    // 任一组的列头都能排序（写回同一个后端 sort 参数）
    await userEvent.click(screen.getByTestId("sort-codebuddy-nickname"));
    expect(fetch).toHaveBeenLastCalledWith(
      expect.stringContaining("sort=nickname"),
      expect.anything(),
    );
  });

  it("没有成长中心的渠道：该列留空占位，列数恒定不错位", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ id: "cb1", provider: "codebuddy", growth_last_result: "领取 30 积分" }),
        makeCredential({ id: "tr1", provider: "trae" }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();

    // CodeBuddy 有该列（可排序列头 + 行内结果）
    expect(screen.getByTestId("sort-codebuddy-growth_last_run_at")).toBeInTheDocument();
    expect(screen.getByTestId("growth-cb1")).toHaveTextContent("领取 30 积分");
    // TRAE 没有：没有列头按钮、没有行内结果
    expect(screen.queryByTestId("sort-trae-growth_last_run_at")).not.toBeInTheDocument();
    expect(screen.queryByTestId("growth-tr1")).not.toBeInTheDocument();

    // 但列数恒定（空占位格顶住），否则 auto 布局会重算列宽、下面展开的组整列错位：
    // 两组的列头 / 数据行都是 7 格（选择 + 5 列 + 操作），且「操作」落在同一格位置
    expect(screen.getByTestId("column-header-codebuddy").children).toHaveLength(7);
    expect(screen.getByTestId("column-header-trae").children).toHaveLength(7);
    expect(screen.getByTestId("row-cb1").children).toHaveLength(7);
    expect(screen.getByTestId("row-tr1").children).toHaveLength(7);
    // 空占位格不承载内容（无排序按钮、无文本）
    const traeHeaderCells = [...screen.getByTestId("column-header-trae").children];
    expect(traeHeaderCells[5]).toBeEmptyDOMElement();
    expect(traeHeaderCells[6]).toHaveTextContent("操作");
    expect(screen.getByTestId("group-codebuddy").querySelector("td")).toHaveAttribute(
      "colspan", "7");
    expect(screen.getByTestId("group-trae").querySelector("td")).toHaveAttribute(
      "colspan", "7");
  });

  it("只读用户：没有操作列，成长中心占位格仍保持列数恒定", async () => {
    mockFetch({
      "/api/credentials": listBody(
        [
          makeCredential({ id: "cb1", provider: "codebuddy", growth_last_result: "领取 30 积分" }),
          makeCredential({ id: "tr1", provider: "trae" }),
        ],
        false,
        false,
      ),
    });
    renderPage(<CredentialsPage />, READER);
    await settle();
    await expandCredentialGroups();

    // 无操作列：总列数是 5（凭证 / 状态 / 额度 / token / 成长中心）
    for (const provider of ["codebuddy", "trae"]) {
      expect(screen.getByTestId(`column-header-${provider}`).children).toHaveLength(5);
      expect(screen.getByTestId(`group-${provider}`).querySelector("td")).toHaveAttribute(
        "colspan", "5");
    }
    expect(screen.getByTestId("row-cb1").children).toHaveLength(5);
    expect(screen.getByTestId("row-tr1").children).toHaveLength(5);
    // 最后一格是成长中心占位格（只读下没有操作列，也没有选择列）
    const traeCells = [...screen.getByTestId("row-tr1").children];
    expect(traeCells[4]).toBeEmptyDOMElement();
    expect(screen.getByTestId("row-cb1").children[4]).toHaveTextContent("领取 30 积分");
  });

  it("搜索按昵称收敛列表并显示计数，无匹配给空态", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ id: "a", nickname: "主力号", provider: "trae" }),
        makeCredential({ id: "b", nickname: "备用号", provider: "trae" }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();

    await userEvent.type(screen.getByTestId("credential-search"), "备用");
    expect(screen.getByTestId("credential-filter-count")).toHaveTextContent("筛选后 1 / 2");
    expect(screen.queryByTestId("row-a")).not.toBeInTheDocument();
    expect(screen.getByTestId("row-b")).toBeInTheDocument();

    await userEvent.clear(screen.getByTestId("credential-search"));
    await userEvent.type(screen.getByTestId("credential-search"), "zzz");
    expect(screen.getByTestId("no-credential-match")).toBeInTheDocument();
    expect(screen.queryByTestId("credentials-table")).not.toBeInTheDocument();
  });

  it("渠道与状态筛选组合收敛，筛选自动展开命中渠道，清空后回到折叠总览", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ id: "cb", provider: "codebuddy" }),
        makeCredential({ id: "tr", provider: "trae" }),
        makeCredential({ id: "tr2", provider: "trae", cooling_until: 9_999_999_999 }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    // 默认全折叠：只有组头，没有数据行
    expect(screen.queryByTestId("row-cb")).not.toBeInTheDocument();
    // 未筛选时没有计数 / 清空入口
    expect(screen.queryByTestId("credential-filter-count")).not.toBeInTheDocument();
    expect(screen.queryByTestId("credential-clear-filters")).not.toBeInTheDocument();

    await userEvent.selectOptions(screen.getByTestId("credential-provider-filter"), "trae");
    expect(screen.getByTestId("credential-filter-count")).toHaveTextContent("筛选后 2 / 3");
    // 筛选生效时自动展开命中渠道，行直接可见
    expect(screen.queryByTestId("row-cb")).not.toBeInTheDocument();
    expect(screen.getByTestId("row-tr")).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByTestId("credential-state-filter"), "cooling");
    expect(screen.getByTestId("credential-filter-count")).toHaveTextContent("筛选后 1 / 3");
    expect(screen.getByTestId("row-tr2")).toBeInTheDocument();
    expect(screen.queryByTestId("row-tr")).not.toBeInTheDocument();

    // 清空筛选回到默认折叠总览：组头在，行收起
    await userEvent.click(screen.getByTestId("credential-clear-filters"));
    expect(screen.queryByTestId("credential-filter-count")).not.toBeInTheDocument();
    expect(screen.getByTestId("group-codebuddy")).toBeInTheDocument();
    expect(screen.queryByTestId("row-cb")).not.toBeInTheDocument();

    // 手动展开后三行都在
    await expandCredentialGroups();
    expect(screen.getByTestId("row-cb")).toBeInTheDocument();
    expect(screen.getByTestId("row-tr")).toBeInTheDocument();
    expect(screen.getByTestId("row-tr2")).toBeInTheDocument();
  });

  it("周期语义收进额度数字的悬浮说明，不再单独占一行", async () => {
    mockFetch({
      "/api/credentials": listBody([makeCredential({ id: "tr", provider: "trae" })]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();

    const row = screen.getByTestId("row-tr");
    // 文案本身不再作为可见文本铺在单元格里
    expect(within(row).queryByText("账户剩余（单调递减）")).not.toBeInTheDocument();
    // 改挂在额度数字的 title 上
    const quota = within(row).getByTitle("账户剩余（单调递减）");
    expect(quota).toHaveTextContent("62 / 100");
  });

  it("标签筛选：下拉按标签收敛，清空后回到全部", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ id: "p", provider: "trae", tags: ["生产"] }),
        makeCredential({ id: "b", provider: "trae", tags: ["备用", "生产"] }),
        makeCredential({ id: "n", provider: "codebuddy", tags: [] }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    expect(screen.getByTestId("row-p")).toBeInTheDocument();

    // 无标签的池不渲染该下拉；这里有两个标签，选项带计数
    const select = screen.getByTestId("credential-tag-filter");
    expect(within(select).getByRole("option", { name: "生产（2）" })).toBeInTheDocument();
    expect(within(select).getByRole("option", { name: "备用（1）" })).toBeInTheDocument();

    await userEvent.selectOptions(select, "生产");
    expect(screen.getByTestId("credential-filter-count")).toHaveTextContent("筛选后 2 / 3");
    expect(screen.getByTestId("row-p")).toBeInTheDocument();
    expect(screen.getByTestId("row-b")).toBeInTheDocument();
    expect(screen.queryByTestId("row-n")).not.toBeInTheDocument();

    await userEvent.selectOptions(select, "备用");
    expect(screen.getByTestId("credential-filter-count")).toHaveTextContent("筛选后 1 / 3");
    expect(screen.getByTestId("row-b")).toBeInTheDocument();
    expect(screen.queryByTestId("row-p")).not.toBeInTheDocument();

    await userEvent.click(screen.getByTestId("credential-clear-filters"));
    expect(screen.queryByTestId("credential-filter-count")).not.toBeInTheDocument();
  });

  it("池里没有任何标签时不渲染标签下拉（不留恒为全部的空控件）", async () => {
    mockFetch({
      "/api/credentials": listBody([makeCredential({ id: "a" })]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    expect(screen.queryByTestId("credential-tag-filter")).not.toBeInTheDocument();
  });

  it("行内最多显示两个标签，多余收成 +N 且 title 给全量", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ id: "many", tags: ["一", "二", "三", "四"] }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();

    const tags = screen.getByTestId("tags-many");
    expect(within(tags).getByText("一")).toBeInTheDocument();
    expect(within(tags).getByText("二")).toBeInTheDocument();
    expect(within(tags).getByText("+2")).toBeInTheDocument();
    expect(within(tags).queryByText("三")).not.toBeInTheDocument();
    expect(tags).toHaveAttribute("title", "一 / 二 / 三 / 四");
  });

  it("无标签的行不渲染标签容器", async () => {
    mockFetch({ "/api/credentials": listBody([makeCredential({ id: "none" })]) });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    expect(screen.queryByTestId("tags-none")).not.toBeInTheDocument();
  });

  it("通过菜单设置标签：保存调用 PUT 并提示，本地先拦超限输入", async () => {
    const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/credentials/cred_1/tags" && init?.method === "PUT") {
        return jsonResponse({ tags: ["生产"] });
      }
      return jsonResponse(listBody([makeCredential({ id: "cred_1", tags: [] })]));
    });
    vi.stubGlobal("fetch", fetchSpy);

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    await userEvent.click(screen.getByTestId("actions-cred_1"));
    await userEvent.click(screen.getByRole("menuitem", { name: "设置标签" }));

    const dialog = await screen.findByTestId("credential-tags-dialog");
    expect(dialog).toHaveTextContent("不影响调度");

    // 超长标签：本地拦住，不发请求
    await userEvent.type(screen.getByTestId("credential-tags-input"), "x".repeat(25));
    await userEvent.click(screen.getByTestId("credential-tags-save"));
    expect(screen.getByTestId("credential-tags-error")).toHaveTextContent("最长 24 个字符");
    expect(fetchSpy).not.toHaveBeenCalledWith(
      "/api/credentials/cred_1/tags",
      expect.objectContaining({ method: "PUT" }),
    );

    // 改成合法标签（含全角逗号）：去重后预览两项，保存发出 PUT
    await userEvent.clear(screen.getByTestId("credential-tags-input"));
    await userEvent.type(screen.getByTestId("credential-tags-input"), "生产，备用，生产");
    const preview = screen.getByTestId("credential-tags-preview");
    expect(preview).toHaveTextContent("生产");
    expect(preview).toHaveTextContent("备用");
    await userEvent.click(screen.getByTestId("credential-tags-save"));

    await waitFor(() =>
      expect(fetchSpy).toHaveBeenCalledWith(
        "/api/credentials/cred_1/tags",
        expect.objectContaining({ method: "PUT" }),
      ),
    );
  });

  it("标签保存失败时提示后端错误，不清空对话框", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url === "/api/credentials/cred_1/tags" && init?.method === "PUT") {
          return jsonResponse({ error: { message: "标签最多 16 条" } }, 400);
        }
        return jsonResponse(listBody([makeCredential({ id: "cred_1" })]));
      }),
    );

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    await userEvent.click(screen.getByTestId("actions-cred_1"));
    await userEvent.click(screen.getByRole("menuitem", { name: "设置标签" }));
    await screen.findByTestId("credential-tags-dialog");

    await userEvent.type(screen.getByTestId("credential-tags-input"), "生产");
    await userEvent.click(screen.getByTestId("credential-tags-save"));

    expect(await screen.findByTestId("credentials-error")).toHaveTextContent("标签最多 16 条");
    expect(screen.getByTestId("credential-tags-dialog")).toBeInTheDocument();
  });

  it("编辑已有标签：输入框预填并支持清空", async () => {
    const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/credentials/cred_1/tags" && init?.method === "PUT") {
        return jsonResponse({ tags: [] });
      }
      return jsonResponse(listBody([makeCredential({ id: "cred_1", tags: ["生产", "备用"] })]));
    });
    vi.stubGlobal("fetch", fetchSpy);

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    await userEvent.click(screen.getByTestId("actions-cred_1"));
    await userEvent.click(screen.getByRole("menuitem", { name: "编辑标签" }));

    const input = await screen.findByTestId("credential-tags-input");
    expect(input).toHaveValue("生产, 备用");

    await userEvent.clear(input);
    await userEvent.click(screen.getByTestId("credential-tags-save"));
    await waitFor(() =>
      expect(fetchSpy).toHaveBeenCalledWith(
        "/api/credentials/cred_1/tags",
        expect.objectContaining({ method: "PUT", body: JSON.stringify({ tags: [] }) }),
      ),
    );
  });

  it("标签对话框可用取消/Esc 关闭且不发请求", async () => {
    const fetchSpy = mockFetch({ "/api/credentials": listBody([makeCredential({ id: "cred_1" })]) });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    await userEvent.click(screen.getByTestId("actions-cred_1"));
    await userEvent.click(screen.getByRole("menuitem", { name: "设置标签" }));
    const input = await screen.findByTestId("credential-tags-input");
    // 先点进输入框：下拉菜单关闭时会异步把焦点还给触发按钮，
    // Esc 若落在对话框之外，焦点陷阱的 keydown 监听收不到。
    await userEvent.click(input);
    await userEvent.keyboard("{Escape}");
    await waitFor(() =>
      expect(screen.queryByTestId("credential-tags-dialog")).not.toBeInTheDocument(),
    );
    // 没有发出标签写请求（Esc 只关闭对话框）
    expect(fetchSpy.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === "PUT"))
      .toBe(false);
  });

  it("只读用户看不到标签设置入口", async () => {
    mockFetch({
      "/api/credentials": listBody([makeCredential({ id: "cred_1", tags: ["生产"] })], false),
    });
    renderPage(<CredentialsPage />, READER);
    await settle();
    await expandCredentialGroups();
    expect(screen.queryByTestId("actions-cred_1")).not.toBeInTheDocument();
    // 但标签徽章与筛选对只读用户可见
    expect(screen.getByTestId("tags-cred_1")).toBeInTheDocument();
    expect(screen.getByTestId("credential-tag-filter")).toBeInTheDocument();
  });
});


