import { screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CredentialsPage } from "../CredentialsPage";
import {
  expandCredentialGroups, jsonResponse, makeCredential, renderPage, settle, userEvent,
  mockCredentialsFetch as mockFetch,
} from "@/test-utils";

const ADMIN = { username: "root", is_admin: true } as const;

function listBody(credentials: unknown[], isAdmin = true, isOperator = isAdmin) {
  return { credentials, expiry_window_seconds: 129600,
            expiry_secondary_window_seconds: 604800,
            token_expiry_warning_seconds: 3600,
            viewer: isAdmin ? "root" : "guest", is_admin: isAdmin,
            is_operator: isOperator };
}

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
    await expandCredentialGroups();
    // 探测 / 签到是常驻按钮，不随渠道变化
    expect(screen.getByTestId("probe-cred_1")).toBeInTheDocument();
    expect(screen.getByTestId("checkin-cred_1")).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("actions-cred_1"));
    expect(screen.queryByRole("menuitem", { name: "成长中心" })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "活跃上报" })).not.toBeInTheDocument();
    // 菜单里不再重复出现探测 / 签到
    expect(screen.queryByRole("menuitem", { name: "探测" })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "签到" })).not.toBeInTheDocument();
  });

  it("CodeBuddy 手动活跃上报：成功/失败各自给出人话反馈", async () => {
    mockFetch({
      "/activity": { ok: false, message: "无法确定账号 userId" },
      "/api/credentials": listBody([makeCredential({ provider: "codebuddy" })]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
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
    await expandCredentialGroups();

    // 成长中心列显示上一轮结果
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

  it("没有成长中心记录时该列显示占位符", async () => {
    mockFetch({ "/api/credentials": listBody([makeCredential({ provider: "codebuddy" })]) });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    expect(screen.queryByTestId("growth-cred_1")).not.toBeInTheDocument();
    // 成长中心是第 6 列（0-based 5，选择列占了 0）；token 剩余列同为「—」，不能按文本全局查
    const row = screen.getByTestId("row-cred_1");
    expect(row.children[5]).toHaveTextContent("—");
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
    await expandCredentialGroups();

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
    await expandCredentialGroups();
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
    await expandCredentialGroups();
    await userEvent.click(screen.getByTestId("actions-cred_1"));
    await userEvent.click(screen.getByRole("menuitem", { name: "成长中心" }));

    expect(await screen.findByTestId("credentials-error")).toHaveTextContent("上游不可用");
    expect(screen.queryByTestId("growth-result")).not.toBeInTheDocument();
  });

  // --------------------------------------------------------- token 到期
  it("token 接近到期：独立一列只显示剩余时间，标红预警", async () => {
    const now = Math.floor(Date.now() / 1000);
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ id: "soon", token_expires_at: now + 600,
                         token_issued_at: now - 30 * 86400 + 600 }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();

    const cell = screen.getByTestId("token-expiry-soon");
    expect(cell).toHaveTextContent("10 分钟");
    expect(cell).toHaveTextContent("即将到期");
    // 表头有独立列名
    expect(screen.getByText("token 剩余")).toBeInTheDocument();
  });

  it("token 剩余列不渲染进度条与「最后续期」", async () => {
    const now = Math.floor(Date.now() / 1000);
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ id: "soon", token_expires_at: now + 600,
                         token_issued_at: now - 30 * 86400 + 600 }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();

    expect(screen.queryByTestId("token-expiry-bar-soon")).not.toBeInTheDocument();
    const row = screen.getByTestId("row-soon");
    expect(within(row).queryByText(/最后续期/)).not.toBeInTheDocument();
  });

  it("token 到期时间未知（0）时该格显示 —，绝不当成已过期", async () => {
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ id: "unknown", token_expires_at: 0, token_issued_at: 0 }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();

    const row = screen.getByTestId("row-unknown");
    expect(within(row).queryByTestId("token-expiry-unknown")).not.toBeInTheDocument();
    expect(within(row).queryByText("已过期")).not.toBeInTheDocument();
    // 与其他空白列一致：显示占位符而不是留空（token 剩余是第 4 列，选择列占了 0）
    const tokenCell = row.children[4];
    expect(tokenCell).toHaveTextContent("—");
  });

  it("token 已过期时明确显示「已过期」", async () => {
    const now = Math.floor(Date.now() / 1000);
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ id: "dead", token_expires_at: now - 60,
                         token_issued_at: now - 30 * 86400 }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();

    expect(screen.getByTestId("token-expiry-dead")).toHaveTextContent("已过期");
  });

  it("阈值之外的 token 只显示剩余时间，不显示预警措辞", async () => {
    const now = Math.floor(Date.now() / 1000);
    mockFetch({
      "/api/credentials": listBody([
        makeCredential({ id: "fresh", token_expires_at: now + 30 * 86400,
                         token_issued_at: now }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();

    const cell = screen.getByTestId("token-expiry-fresh");
    expect(cell).toHaveTextContent("30.0 天");
    expect(cell).not.toHaveTextContent("即将到期");
  });

  // --------------------------------------------------------- 积分记录
  it("积分记录抽屉：展示净变化与区间起点，并说明不是动作归因", async () => {
    const now = Math.floor(Date.now() / 1000);
    mockFetch({
      // credit-events 路径也含 "/api/credentials"，必须放在列表路由前
      "/credit-events": { events: [
        { id: "cre_2", credential_id: "cred_1", ts: now, window_start: now - 600,
          before: 100, after: 115, delta: 15, source: "observed" },
        { id: "cre_1", credential_id: "cred_1", ts: now - 600, window_start: null,
          before: null, after: 100, delta: null, source: "sync" },
      ] },
      "/api/credentials": listBody([
        makeCredential({
          id: "cred_1",
          quota_remaining: 4872.77,
          quota_total: 4950,
          // 放一个额度包：用它锚定「积分记录箭头排在额度包之前」的相对顺序
          quota_packages: [{ name: "福利积分", total: 100, used: 0, end: null }],
        }),
      ]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();

    await userEvent.click(screen.getByTestId("credits-cred_1"));

    const drawer = await screen.findByTestId("credit-drawer-cred_1");
    // 说「净变化」，不说「签到获得」——上游不打日志，归因是猜的
    expect(drawer).toHaveTextContent("两次额度探测之间的净变化（非动作归因）");
    // 入口是额度列数字后的下箭头：它是「行内展开」不是弹层，
    // aria-expanded 与箭头翻转同步反映展开态。
    const toggle = screen.getByTestId("credits-cred_1");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle.querySelector("svg")).toHaveClass("rotate-180");
    // 位置锁死在积分数字正后方（「套餐 N 个」之前），不是排在行尾；
    // 且与数字同在一个 inline-flex items-center 容器里——图标按钮没有文字
    // 基线，跟整行 baseline 对齐会明显偏低。
    const quotaLine = toggle.parentElement as HTMLElement;
    expect(quotaLine).toHaveClass("items-center");
    // 不要自作聪明加 translate 补偿：实测「数字字形 4」的墨迹中心与箭头墨迹
    // 中心完全重合（都在 267.144px）。之前那次「偏低」其实是多余的
    // translate-y-0.5 自己造成的。这里用计算样式断言，静止时不允许有位移
    //（按钮基础样式自带 active:translate-y-px 的按下反馈，不属于对齐补偿）。
    expect(getComputedStyle(toggle).transform).toBe("none");
    expect(quotaLine.parentElement?.children[1]).toHaveTextContent("套餐");
    expect(quotaLine.firstElementChild).toHaveTextContent("4,872.77 / 4,950");
    expect(quotaLine.children[1]).toBe(toggle);
    expect(quotaLine.parentElement).toHaveTextContent("4,872.77 / 4,950");
    // 抽屉必须是独立的表格行：塞进凭证行内部只会挤在「操作」列里
    // （同行内 colSpan 不生效），这个断言就是防那次布局事故复发
    const drawerRow = screen.getByTestId("credit-row-cred_1");
    expect(drawerRow.tagName).toBe("TR");
    expect(drawerRow).not.toBe(screen.getByTestId("row-cred_1"));
    expect(within(drawerRow).getByTestId("credit-drawer-cred_1")).toBeInTheDocument();
    // :scope > td：抽屉内部还有一张表格，直接按 role 查会匹配到里面那些单元格。
    // 抽屉的 colSpan 与组头一致，是「表格总列数」而非本组的单元格个数
    expect(drawerRow.querySelector(":scope > td")).toHaveAttribute("colspan", "7");
    expect(within(drawer).getByTestId("credit-event-cre_2")).toHaveTextContent(
      "+15（100 → 115）",
    );
    expect(within(drawer).getByTestId("credit-event-cre_1")).toHaveTextContent("基线 100");
    expect(within(drawer).getByTestId("credit-event-cre_1")).toHaveTextContent(
      "首次建立基线",
    );
  });

  it("积分记录抽屉：变化无法量化时如实说「变为未知」，不显示成 0", async () => {
    const now = Math.floor(Date.now() / 1000);
    mockFetch({
      "/credit-events": { events: [
        { id: "cre_x", credential_id: "cred_1", ts: now, window_start: now - 60,
          before: 100, after: null, delta: null, source: "observed" },
      ] },
      "/api/credentials": listBody([makeCredential({ id: "cred_1" })]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    await userEvent.click(screen.getByTestId("credits-cred_1"));

    const cell = await screen.findByTestId("credit-event-cre_x");
    expect(cell).toHaveTextContent("由 100 变为未知");
    // 变化列绝不能渲染成一个 0（会被读成「没有变化」）
    const changeCell = cell.querySelector("td:nth-child(3)");
    expect(changeCell?.textContent).toBe("由 100 变为未知");
  });

  it("积分记录抽屉：无记录给出去哪看说明，并可关闭/再开", async () => {
    mockFetch({
      "/credit-events": { events: [] },
      "/api/credentials": listBody([makeCredential({ id: "cred_1" })]),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();

    await userEvent.click(screen.getByTestId("credits-cred_1"));
    const drawer = await screen.findByTestId("credit-drawer-cred_1");
    expect(within(drawer).getByTestId("credit-drawer-empty")).toHaveTextContent(
      "下一轮额度探测时写入",
    );

    // 关闭后箭头转回朝下
    await userEvent.click(screen.getByTestId("credit-drawer-close"));
    expect(screen.queryByTestId("credit-drawer-cred_1")).not.toBeInTheDocument();
    expect(screen.getByTestId("credits-cred_1")).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByTestId("credits-cred_1").querySelector("svg")).not.toHaveClass(
      "rotate-180",
    );

    // 再次点箭头可重新打开（不是一次性开关）
    await userEvent.click(screen.getByTestId("credits-cred_1"));
    expect(await screen.findByTestId("credit-drawer-cred_1")).toBeInTheDocument();
  });
});
