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
  avg_latency_ms: 850,
  avg_ttfb_ms: 220,
};

const PROVIDERS = {
  providers: [
    { provider: "trae", requests: 70, ok_count: 68, input_tokens: 600, output_tokens: 1200, credit: null },
    { provider: "codebuddy", requests: 50, ok_count: 46, input_tokens: 400, output_tokens: 800, credit: 12.5 },
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
      credit: 0.35, latency_ms: 8200, ttfb_ms: 2400 },
    { rowid: 2, ts: BASE_TS + 100, username: "root", provider: "trae", model: "deepseek-v4",
      credential_id: null, credential_name: null,
      ok: 0, error_type: "rate_limit", input_tokens: null, output_tokens: null, cached_tokens: null,
      credit: null, latency_ms: 460, ttfb_ms: 120 },
  ],
  next_before: 2,
};

const EVENTS_PAGE2 = {
  events: [
    { rowid: 1, ts: BASE_TS, username: "root", provider: "trae", model: "kimi-k3",
      ok: 1, error_type: null, input_tokens: 5, output_tokens: 9, cached_tokens: null,
      credit: null, latency_ms: 300, ttfb_ms: 150 },
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
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": EVENTS_EMPTY,
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    expect(screen.getByTestId("stats-page")).toHaveTextContent("120");
    // 成功率并入请求数卡片 hint；Token 三项并入 Token 消耗卡片
    expect(screen.getByText("成功率 95.0%")).toBeInTheDocument();
    expect(screen.getByText(/输入 1000（命中 400 · 未命中 600） · 输出 2000 · 推理 300/))
      .toBeInTheDocument();
    expect(screen.getByText(/首字延迟 220 ms/)).toBeInTheDocument();
    expect(screen.getByTestId("provider-table")).toHaveTextContent("CodeBuddy");
    expect(screen.getByTestId("provider-table")).toHaveTextContent("TRAE");
  });

  it("按模型趋势面板与 Top 文案渲染", async () => {
    mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
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
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": EVENTS_EMPTY,
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    // 总消耗 8013579 → 801.4万；分项 123456 → 12.3万、7890123 → 789万
    expect(screen.getByText("801.4万")).toBeInTheDocument();
    expect(screen.getByText(/输入 12.3万（命中 — · 未命中 —） · 输出 789万 · 推理 300/))
      .toBeInTheDocument();
  });

  it("credit 为 null 时显示占位符而非 0", async () => {
    mockFetch({
      "/api/stats/overview": { ...OVERVIEW, credit: null },
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": EVENTS_EMPTY,
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    // TRAE 那一行的 credit 为 null（限定表格范围：图例 logo 的 title 也叫 TRAE）
    const table = screen.getByTestId("provider-table");
    const row = within(table).getAllByText("TRAE")[0].closest("tr")!;
    expect(row).toHaveTextContent("—");
    // 总览未探测到 credit
    expect(screen.getByText("Credit 消耗").closest("[data-slot=card]")).toHaveTextContent("—");
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
      "/api/stats/model-timeline": { models: [], points: [] },
      "/api/stats/events": EVENTS_EMPTY,
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();
    expect(screen.getByText("请求数").closest("[data-slot=card]")).toHaveTextContent("成功率 —");
    expect(screen.getByText("平均耗时").closest("[data-slot=card]")).toHaveTextContent("首字延迟 —");
    expect(screen.getByTestId("no-provider-stats")).toBeInTheDocument();
    expect(screen.getByTestId("no-model-trend")).toBeInTheDocument();
  });

      it("切换时间范围触发带 since 的新查询", async () => {
    const fetchSpy = mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
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
  });

  it("请求明细面板渲染成功与失败状态，管理员见用户列", async () => {
    mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
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
    expect(within(table).getByText("rate_limit")).toBeInTheDocument();
    expect(within(table).getByText("8.2 s")).toBeInTheDocument();
    expect(within(table).getByText("2.4 s")).toBeInTheDocument();
    expect(within(table).getByText("90")).toBeInTheDocument();
    expect(within(table).getByText("460 ms")).toBeInTheDocument();
    expect(within(table).getByText("120 ms")).toBeInTheDocument();
    expect(screen.getByTestId("events-prev")).toBeDisabled();
    expect(screen.getByTestId("events-next")).toBeEnabled();
  });

  it("请求明细分页：下一页按 before 取数，上一页回退，换每页数量重置", async () => {
    const fetchSpy = mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
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

  it("非管理员明细表无用户列", async () => {
    mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": EVENTS_PAGE1,
    });
    renderPage(<StatsPage />, READER);
    await settle();

    const table = screen.getByTestId("events-table");
    expect(within(table).queryByText("用户")).not.toBeInTheDocument();
    expect(within(table).queryByText("root")).not.toBeInTheDocument();
  });
});
