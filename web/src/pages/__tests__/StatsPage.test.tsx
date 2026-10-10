import { screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { StatsPage } from "../StatsPage";
import { jsonResponse, mockFetch, renderPage, settle, userEvent } from "./helpers";

const ADMIN = { username: "root", is_admin: true } as const;
const READER = { username: "alice", is_admin: false } as const;

const OVERVIEW = {
  requests: 120,
  ok_count: 114,
  success_rate: 0.95,
  input_tokens: 1000,
  output_tokens: 2000,
  reasoning_tokens: 300,
  cached_tokens: 400,
  credit: 12.5,
  credit_estimated: false,
  cost_usd: 1.84,
  cost_cny: 12.34,
  avg_latency_ms: 850,
  avg_ttfb_ms: 220,
};

const PROVIDERS = {
  providers: [
    { provider: "trae", requests: 70, ok_count: 68, input_tokens: 600, output_tokens: 1200, cached_tokens: 250, credit: 5.25, credit_estimated: true, cost_usd: 0.5, cost_cny: 3.35 },
    { provider: "codebuddy", requests: 50, ok_count: 46, input_tokens: 400, output_tokens: 800, cached_tokens: 150, credit: 12.5, credit_estimated: false, cost_usd: 1.34, cost_cny: 8.99 },
  ],
};

const MODELS = {
  models: [
    { model: "glm-5.2", requests: 40, ok_count: 38, input_tokens: 300, output_tokens: 900, cached_tokens: 100, credit: 8.0, credit_estimated: false, cost_usd: 0.8, cost_cny: 5.36 },
    { model: "kimi-k3", requests: 30, ok_count: 28, input_tokens: 200, output_tokens: 600, credit: 4.5, credit_estimated: true, cost_usd: 0.4, cost_cny: 2.68 },
  ],
};

const USERS = {
  users: [
    { username: "alice", requests: 80, ok_count: 76, input_tokens: 700, output_tokens: 1500, cached_tokens: 300, credit: 9.75, credit_estimated: true, cost_usd: 1.2, cost_cny: 8.04 },
    { username: "bob", requests: 40, ok_count: 38, input_tokens: 300, output_tokens: 500, cached_tokens: 100, credit: 2.75, credit_estimated: false, cost_usd: 0.64, cost_cny: 4.28 },
  ],
};

const CREDENTIALS = {
  credentials: [
    { credential_id: "cred_abc123", credential_name: "主账号", provider: "trae",
      requests: 70, ok_count: 68, input_tokens: 600, output_tokens: 1200, cached_tokens: 250, credit: 5.25, credit_estimated: true, cost_usd: 0.5, cost_cny: 3.35 },
    // 无昵称（老库/已删除）：回落 ID 前 12 位
    { credential_id: "cred_zzz999888777", credential_name: null, provider: "codebuddy",
      requests: 50, ok_count: 46, input_tokens: 400, output_tokens: 800, cached_tokens: 150, credit: 12.5, credit_estimated: false, cost_usd: 1.34, cost_cny: 8.99 },
  ],
};

const MODEL_TIMELINE = {
  models: ["glm-5.2", "kimi-k3"],
  points: [
    { hour: 1700000000, "glm-5.2": 4, "kimi-k3": 1 },
    { hour: 1700003600, "glm-5.2": 0, "kimi-k3": 2 },
  ],
};

const EVENTS_EMPTY = { events: [], next_before: null };
const BASE_TS = 1_700_000_000;

const EVENTS_PAGE1 = {
  events: [
    { rowid: 3, ts: BASE_TS + 200, username: "root", provider: "codebuddy", model: "glm-5.2",
      credential_id: "cred_abc123", credential_name: "主账号",
      ok: 1, error_type: null, input_tokens: 120, output_tokens: 480, cached_tokens: 90,
      credit: 0.35, credit_estimated: 0, cost_usd: 0.0012, cost_cny: 0.008,
      latency_ms: 8200, ttfb_ms: 2400 },
    { rowid: 2, ts: BASE_TS + 100, username: "root", provider: "trae", model: "deepseek-v4",
      credential_id: null, credential_name: null,
      ok: 0, error_type: "rate_limit", input_tokens: null, output_tokens: null, cached_tokens: null,
      credit: null, credit_estimated: 0, cost_usd: null, cost_cny: null,
      latency_ms: 460, ttfb_ms: 120 },
  ],
  next_before: 2,
};

const EVENTS_PAGE2 = {
  events: [
    { rowid: 1, ts: BASE_TS, username: "root", provider: "trae", model: "kimi-k3",
      ok: 1, error_type: null, input_tokens: 5, output_tokens: 9, cached_tokens: null,
      credit: null, credit_estimated: 0, cost_usd: null, cost_cny: null,
      latency_ms: 300, ttfb_ms: 150 },
  ],
  next_before: null,
};

describe("StatsPage", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("渲染合并后的总览指标与按渠道分组", async () => {
    mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": EVENTS_EMPTY,
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    expect(screen.getByTestId("stats-page")).toHaveTextContent("120");
    // 成功率并入请求数卡片 hint；Token 三项并入 Token 消耗卡片
    expect(screen.getByText("成功率 95.0%")).toBeInTheDocument();
    expect(screen.getByText(/输入 1000（命中 400 · 未命中 600） · 输出 2000 · 推理 300 · 缓存命中率 40.0%/))
      .toBeInTheDocument();
    expect(screen.getByText(/首字延迟 220 ms/)).toBeInTheDocument();
    expect(screen.getByTestId("group-table")).toHaveTextContent("CodeBuddy");
    expect(screen.getByTestId("group-table")).toHaveTextContent("TRAE");
  });

  it("按模型趋势面板与 Top 文案渲染", async () => {
    mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": EVENTS_EMPTY,
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    expect(screen.getByText("按模型趋势")).toBeInTheDocument();
    expect(screen.getByText("请求量 Top 2 模型")).toBeInTheDocument();
    expect(screen.getByTestId("model-trend-chart")).toBeInTheDocument();
  });

  it("token 大数用紧凑格式显示（万）", async () => {
    mockFetch({
      "/api/stats/overview": { ...OVERVIEW, input_tokens: 123456, output_tokens: 7890123,
        cached_tokens: null },
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": EVENTS_EMPTY,
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    // 总消耗 8013579 → 801.4万；分项 123456 → 12.3万、7890123 → 789万
    expect(screen.getByText("801.4万")).toBeInTheDocument();
    expect(screen.getByText(/输入 12.3万（命中 — · 未命中 —） · 输出 789万 · 推理 300 · 缓存命中率 —/))
      .toBeInTheDocument();
  });

  it("credit 为 null 时显示占位符而非 0", async () => {
    mockFetch({
      "/api/stats/overview": { ...OVERVIEW, credit: null, credit_estimated: false },
      "/api/stats/by-provider": { providers: [
        { provider: "trae", requests: 70, ok_count: 68, input_tokens: 600, output_tokens: 1200,
          credit: null, credit_estimated: false, cost_usd: null, cost_cny: null },
      ] },
      "/api/stats/by-model": { models: [] },
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": EVENTS_EMPTY,
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    // TRAE 那一行的 credit 为 null（限定表格范围：图例 logo 的 title 也叫 TRAE）
    const table = screen.getByTestId("group-table");
    const row = within(table).getAllByText("TRAE")[0].closest("tr")!;
    expect(row).toHaveTextContent("—");
    // 总览未探测到 credit
    expect(screen.getByText("Credit 消耗").closest("[data-slot=card]")).toHaveTextContent("—");
  });

  it("推算 credit 显示 ≈ 前缀（TRAE 无上游积分）", async () => {
    mockFetch({
      "/api/stats/overview": { ...OVERVIEW, credit: 5.25, credit_estimated: true },
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": EVENTS_EMPTY,
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    // 总览卡片与按渠道表对推算值都标 ≈
    expect(screen.getByText("Credit 消耗").closest("[data-slot=card]")).toHaveTextContent("≈5.25");
    const table = screen.getByTestId("group-table");
    const traeRow = within(table).getAllByText("TRAE")[0].closest("tr")!;
    expect(traeRow).toHaveTextContent("≈5.25");
    const cbRow = within(table).getAllByText("CodeBuddy")[0].closest("tr")!;
    expect(cbRow).toHaveTextContent("12.5");
    expect(cbRow).not.toHaveTextContent("≈12.5");
  });

  it("成本卡片：人民币为主、美元为辅，未匹配定价显示 —", async () => {
    mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": EVENTS_EMPTY,
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    const card = screen.getByText("成本（估算）").closest("[data-slot=card]")!;
    expect(card).toHaveTextContent("≈¥12.34");
    expect(card).toHaveTextContent("美元 ≈$1.84");
    // 按渠道表带成本列，人民币口径
    const table = screen.getByTestId("group-table");
    const traeRow = within(table).getAllByText("TRAE")[0].closest("tr")!;
    expect(traeRow).toHaveTextContent("≈¥3.35");
  });

  it("成本缺失（无可定价模型）显示占位符而非 ¥0", async () => {
    mockFetch({
      "/api/stats/overview": { ...OVERVIEW, cost_usd: null, cost_cny: null },
      "/api/stats/by-provider": { providers: [
        { provider: "trae", requests: 70, ok_count: 68, input_tokens: 600, output_tokens: 1200,
          credit: 5.25, credit_estimated: true, cost_usd: null, cost_cny: null },
      ] },
      "/api/stats/by-model": { models: [] },
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": EVENTS_EMPTY,
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    expect(screen.getByText("成本（估算）").closest("[data-slot=card]")).toHaveTextContent("—");
    const table = screen.getByTestId("group-table");
    const row = within(table).getAllByText("TRAE")[0].closest("tr")!;
    expect(row).toHaveTextContent("—");
  });

  it("成功率与延迟缺失时显示占位符", async () => {
    mockFetch({
      "/api/stats/overview": {
        ...OVERVIEW,
        requests: 0,
        ok_count: 0,
        success_rate: null,
        avg_latency_ms: null,
        avg_ttfb_ms: null,
        cached_tokens: null,
      },
      "/api/stats/by-provider": { providers: [] },
      "/api/stats/by-model": { models: [] },
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": { models: [], points: [] },
      "/api/stats/events": EVENTS_EMPTY,
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();
    expect(screen.getByText("请求数").closest("[data-slot=card]")).toHaveTextContent("成功率 —");
    expect(screen.getByText("平均耗时").closest("[data-slot=card]")).toHaveTextContent("首字延迟 —");
    expect(screen.getByTestId("no-group-stats")).toBeInTheDocument();
    expect(screen.getByTestId("no-model-trend")).toBeInTheDocument();
  });

      it("切换时间范围触发带 since 的新查询", async () => {
    const fetchSpy = mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": EVENTS_EMPTY,
    });
    renderPage(<StatsPage />, READER);
    await settle();

    expect(screen.getByTestId("range-tabs-7d")).toHaveAttribute("aria-selected", "true");
    await userEvent.click(screen.getByTestId("range-tabs-24h"));
    // React 会重建 tablist 节点，断言必须每次现查，不能缓存元素引用
    await waitFor(() =>
      expect(screen.getByTestId("range-tabs-24h")).toHaveAttribute("aria-selected", "true"),
    );
    await waitFor(() =>
      expect(fetchSpy.mock.calls.some(([url]) => String(url).includes("since="))).toBe(true),
    );
  });

    it("选择「全部」时不带 since 参数", async () => {
    const fetchSpy = mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": EVENTS_EMPTY,
    });
    renderPage(<StatsPage />, READER);
    await settle();
    fetchSpy.mockClear();

    await userEvent.click(screen.getByTestId("range-tabs-all"));
    await waitFor(() => expect(fetchSpy).toHaveBeenCalled());
    expect(fetchSpy.mock.calls.every(([url]) => !String(url).includes("since="))).toBe(true);
  });

  it("切换图表指标触发带 metric 的查询（默认请求次数）", async () => {
    const fetchSpy = mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": EVENTS_EMPTY,
    });
    renderPage(<StatsPage />, READER);
    await settle();
    // 默认选中请求次数；初始请求带 metric=requests
    expect(screen.getByTestId("metric-tabs-requests")).toHaveAttribute("aria-selected", "true");
    expect(fetchSpy.mock.calls.some(([url]) =>
      String(url).includes("metric=requests"))).toBe(true);
    fetchSpy.mockClear();

    await userEvent.click(screen.getByTestId("metric-tabs-tokens"));
    await waitFor(() =>
      expect(fetchSpy.mock.calls.some(([url]) =>
        String(url).includes("metric=tokens"))).toBe(true),
    );
    expect(screen.getByTestId("metric-tabs-tokens")).toHaveAttribute("aria-selected", "true");

    // 成本指标也可选：触发 metric=cost（人民币口径）
    await userEvent.click(screen.getByTestId("metric-tabs-cost"));
    await waitFor(() =>
      expect(fetchSpy.mock.calls.some(([url]) =>
        String(url).includes("metric=cost"))).toBe(true),
    );
    expect(screen.getByTestId("metric-tabs-cost")).toHaveAttribute("aria-selected", "true");
  });

  it("请求明细：成本列显示人民币，缺失显示 —", async () => {
    mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": EVENTS_PAGE1,
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    const table = screen.getByTestId("events-table");
    expect(within(table).getByText("成本")).toBeInTheDocument();
    expect(within(table).getByText("≈¥0.008")).toBeInTheDocument();
  });

  it("请求明细：推算 credit 标 ≈，真实 credit 不标", async () => {
    mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": {
        events: [
          { rowid: 2, ts: BASE_TS + 100, username: "root", provider: "trae",
            model: "deepseek-v4.1-flash", credential_id: null, credential_name: null,
            ok: 1, error_type: null, input_tokens: 1000, output_tokens: 200,
            cached_tokens: null, credit: 0.05, credit_estimated: 1,
            cost_usd: null, cost_cny: null, latency_ms: 400, ttfb_ms: 120 },
          { rowid: 1, ts: BASE_TS, username: "root", provider: "codebuddy",
            model: "glm-5.2", credential_id: null, credential_name: null,
            ok: 1, error_type: null, input_tokens: 10, output_tokens: 5,
            cached_tokens: null, credit: 1.5, credit_estimated: 0,
            cost_usd: null, cost_cny: null, latency_ms: 300, ttfb_ms: 150 },
        ],
        next_before: null,
      },
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    const table = screen.getByTestId("events-table");
    expect(within(table).getByText("≈0.05")).toBeInTheDocument();
    expect(within(table).getByText("1.5")).toBeInTheDocument();
    expect(within(table).queryByText("≈1.5")).not.toBeInTheDocument();
  });

  it("请求明细面板渲染成功与失败状态，管理员见用户列", async () => {
    mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": EVENTS_PAGE1,
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    const table = screen.getByTestId("events-table");
    expect(within(table).getByText("用户")).toBeInTheDocument();
    expect(within(table).getByText("凭证")).toBeInTheDocument();
    expect(within(table).getByText("主账号")).toBeInTheDocument();
    expect(within(table).getByText("glm-5.2")).toBeInTheDocument();
    expect(within(table).getByText("成功")).toBeInTheDocument();
    expect(within(table).getByText("额度耗尽")).toBeInTheDocument();
    expect(within(table).getByText("8.2 s")).toBeInTheDocument();
    expect(within(table).getByText("2.4 s")).toBeInTheDocument();
    expect(within(table).getByText("90")).toBeInTheDocument();
    expect(within(table).getByText("460 ms")).toBeInTheDocument();
    expect(within(table).getByText("120 ms")).toBeInTheDocument();
    expect(screen.getByTestId("events-prev")).toBeDisabled();
    expect(screen.getByTestId("events-next")).toBeEnabled();
  });

  it("运维角色看全量统计，用户列与管理员一致可见", async () => {
    mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": EVENTS_PAGE1,
    });
    renderPage(<StatsPage />, { ...READER, is_operator: true, role: "operator" });
    await settle();

    expect(within(screen.getByTestId("events-table")).getByText("用户")).toBeInTheDocument();
  });

  it("请求明细分页：下一页按 before 取数，上一页回退，换每页数量重置", async () => {
    const fetchSpy = mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": (url: string) =>
        jsonResponse(url.includes("before=") ? EVENTS_PAGE2 : EVENTS_PAGE1),
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();
    expect(screen.getByTestId("events-table")).toHaveTextContent("glm-5.2");
    expect(screen.getByTestId("events-page-info")).toHaveTextContent("第 1 页");

    await userEvent.click(screen.getByTestId("events-next"));
    await waitFor(() =>
      expect(fetchSpy.mock.calls.some(([url]) => String(url).includes("before=2"))).toBe(true),
    );
    // 跨过 1 秒再断言：since 锚定后翻页状态不会被「范围变化重置」打回第一页
    await new Promise((resolve) => setTimeout(resolve, 1200));
    expect(screen.getByTestId("events-table")).toHaveTextContent("kimi-k3");
    expect(screen.getByTestId("events-page-info")).toHaveTextContent("第 2 页");
    expect(screen.getByTestId("events-next")).toBeDisabled();          // 到底
    expect(screen.getByTestId("events-prev")).toBeEnabled();
    // 不带 before 的首页请求只应出现过一次（无重置抖动）
    const firstPageCalls = fetchSpy.mock.calls.filter(
      ([url]) => String(url).includes("/api/stats/events") && !String(url).includes("before="),
    );
    expect(firstPageCalls).toHaveLength(1);

    await userEvent.click(screen.getByTestId("events-prev"));
    await waitFor(() =>
      expect(screen.getByTestId("events-table")).toHaveTextContent("glm-5.2"),
    );

    await userEvent.selectOptions(screen.getByTestId("events-page-size"), "50");
    await waitFor(() =>
      expect(fetchSpy.mock.calls.some(([url]) => String(url).includes("limit=50"))).toBe(true),
    );
    expect(screen.getByTestId("events-page-info")).toHaveTextContent("第 1 页");

    // 翻到第 2 页后切时间范围 → 事件驱动重置回第 1 页（且不因 since 漂移误重置）
    await userEvent.click(screen.getByTestId("events-next"));
    await waitFor(() =>
      expect(screen.getByTestId("events-page-info")).toHaveTextContent("第 2 页"),
    );
    await new Promise((resolve) => setTimeout(resolve, 1200));
    await userEvent.click(screen.getByTestId("range-tabs-24h"));
    await waitFor(() =>
      expect(screen.getByTestId("events-page-info")).toHaveTextContent("第 1 页"),
    );
    expect(screen.getByTestId("events-table")).toHaveTextContent("glm-5.2");
  });

  it("分组统计排序：默认请求数降序，点表头带 sort/order 重新取数", async () => {
    const spy = mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": EVENTS_EMPTY,
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    // 默认按请求数降序（后端默认，前端也显式下发）
    const providerCall = spy.mock.calls
      .map((call) => String(call[0]))
      .find((url) => url.includes("/api/stats/by-provider"))!;
    expect(providerCall).toContain("sort=requests");
    expect(providerCall).toContain("order=desc");

    // 点「渠道」列头 → 按分组键升序重新取数
    await userEvent.click(screen.getByTestId("sort-group"));
    await waitFor(() => {
      const last = spy.mock.calls
        .map((call) => String(call[0]))
        .filter((url) => url.includes("/api/stats/by-provider"))
        .at(-1)!;
      expect(last).toContain("sort=group");
    });
  });

  it("请求明细排序：换列从 rowid 游标切到 offset 分页", async () => {
    const spy = mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": { events: EVENTS_PAGE1.events, next_before: null, total: 7 },
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    // 默认（rowid 降序）走游标分页：请求不带 offset
    const first = spy.mock.calls.map((call) => String(call[0]))
      .find((url) => url.includes("/api/stats/events"))!;
    expect(first).toContain("sort=time");
    expect(first).not.toContain("offset=");

    // 点「模型」列头 → 走 offset 分页（offset 从 0 起）
    await userEvent.click(screen.getByTestId("event-sort-model"));
    await waitFor(() => {
      const last = spy.mock.calls.map((call) => String(call[0]))
        .filter((url) => url.includes("/api/stats/events")).at(-1)!;
      expect(last).toContain("sort=model");
      expect(last).toContain("offset=0");
    });
    // total=7 > 本页 2 条 → 下一页可用（offset 模式的 hasNextPage）
    expect(screen.getByTestId("events-next")).toBeEnabled();
  });

  it("非管理员明细表无用户列", async () => {
    mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": EVENTS_PAGE1,
    });
    renderPage(<StatsPage />, READER);
    await settle();

    const table = screen.getByTestId("events-table");
    expect(within(table).queryByText("用户")).not.toBeInTheDocument();
    expect(within(table).queryByText("root")).not.toBeInTheDocument();
  });

  it("分组统计：默认按渠道，切换 tabs 依次展示模型/用户/凭证", async () => {
    mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": EVENTS_EMPTY,
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    // 默认选中「按渠道」
    expect(screen.getByTestId("group-tabs-provider")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("group-table")).toHaveTextContent("TRAE");
    expect(screen.getByTestId("group-table")).toHaveTextContent("CodeBuddy");

    // 切模型：行换成模型名
    await userEvent.click(screen.getByTestId("group-tabs-model"));
    await waitFor(() => expect(within(screen.getByTestId("group-table")).getByText("glm-5.2"))
      .toBeInTheDocument());
    expect(screen.getByTestId("group-table")).toHaveTextContent("kimi-k3");

    // 切用户
    await userEvent.click(screen.getByTestId("group-tabs-user"));
    await waitFor(() => expect(within(screen.getByTestId("group-table")).getByText("alice"))
      .toBeInTheDocument());
    expect(screen.getByTestId("group-table")).toHaveTextContent("bob");

    // 切凭证：昵称优先（无昵称回落到 ID 前 12 位）
    await userEvent.click(screen.getByTestId("group-tabs-credential"));
    await waitFor(() => expect(screen.getByTestId("group-table"))
      .toHaveTextContent("主账号"));
    expect(screen.getByTestId("group-table")).toHaveTextContent("cred_zzz9998");
    expect(within(screen.getByTestId("group-table")).queryByText("cred_zzz999888777"))
      .not.toBeInTheDocument();
  });

  it("分组统计：各维度都给出成功率、命中缓存与成本", async () => {
    mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": EVENTS_EMPTY,
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    // 按模型：glm-5.2 38/40 = 95.0%，命中缓存 100，成本 ¥5.36
    await userEvent.click(screen.getByTestId("group-tabs-model"));
    await settle();
    let row = within(screen.getByTestId("group-table")).getByText("glm-5.2").closest("tr")!;
    expect(row).toHaveTextContent("95.0%");
    expect(row).toHaveTextContent("100");
    expect(row).toHaveTextContent("¥5.36");
    // kimi-k3 28/30 = 93.3%，推算成本标 ≈
    row = within(screen.getByTestId("group-table")).getByText("kimi-k3").closest("tr")!;
    expect(row).toHaveTextContent("93.3%");
    expect(row).toHaveTextContent("≈¥2.68");

    // 按用户：alice 76/80 = 95.0%
    await userEvent.click(screen.getByTestId("group-tabs-user"));
    await settle();
    row = within(screen.getByTestId("group-table")).getByText("alice").closest("tr")!;
    expect(row).toHaveTextContent("95.0%");
    expect(row).toHaveTextContent("¥8.04");

    // 按凭证：主账号行 68/70 = 97.1%
    await userEvent.click(screen.getByTestId("group-tabs-credential"));
    await settle();
    row = within(screen.getByTestId("group-table")).getByText("主账号").closest("tr")!;
    expect(row).toHaveTextContent("97.1%");
    expect(row).toHaveTextContent("≈¥3.35");
  });

  it("分组统计：凭证名与请求明细同口径，且带渠道 icon", async () => {
    mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": EVENTS_PAGE1,
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    await userEvent.click(screen.getByTestId("group-tabs-credential"));
    await settle();

    const groupTable = screen.getByTestId("group-table");
    // 同一个凭证：分组表与明细表都显示昵称「主账号」，完整 ID 落在 title 里
    const groupCell = within(groupTable).getByText("主账号").closest("td")!;
    expect(groupCell).toHaveAttribute("title", "cred_abc123");
    const eventCell = within(screen.getByTestId("events-table")).getByText("主账号")
      .closest("td")!;
    expect(eventCell).toHaveAttribute("title", "cred_abc123");
    // 凭证名前有渠道 icon（品牌 svg）
    expect(groupCell.querySelector("svg")).not.toBeNull();
    // 无昵称的行回落 ID 前 12 位，title 给完整 ID
    const fallbackCell = within(groupTable).getByText("cred_zzz9998").closest("td")!;
    expect(fallbackCell).toHaveAttribute("title", "cred_zzz999888777");
    expect(fallbackCell.querySelector("svg")).not.toBeNull();
  });

  it("分组统计：只读用户看不到按用户/按凭证 tab", async () => {
    mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": EVENTS_EMPTY,
    });
    renderPage(<StatsPage />, READER);
    await settle();

    expect(screen.getByTestId("group-tabs-provider")).toBeInTheDocument();
    expect(screen.getByTestId("group-tabs-model")).toBeInTheDocument();
    expect(screen.queryByTestId("group-tabs-user")).not.toBeInTheDocument();
    expect(screen.queryByTestId("group-tabs-credential")).not.toBeInTheDocument();
  });
});
