import { screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { StatsPage } from "../StatsPage";
import { mockFetch, renderPage, settle, userEvent } from "@/test-utils";
import {
  ADMIN,
  BASE_TS,
  CREDENTIALS,
  EVENTS_EMPTY,
  EVENTS_PAGE1,
  MODEL_TIMELINE,
  MODELS,
  OVERVIEW,
  PERCENTILE_TIMELINE,
  PROVIDERS,
  READER,
  TIMELINE,
  USERS,
} from "@/test-utils/stats";

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
    expect(screen.getByText("成功 114 · 成功率 95.0%")).toBeInTheDocument();
    expect(screen.getByText(/输入 1000（命中 400 · 未命中 600） · 输出 2000 · 推理 300 · 缓存命中率 40.0%/))
      .toBeInTheDocument();
    expect(screen.getByText(/首字延迟 220 ms/)).toBeInTheDocument();
    // 输出速度：正文 token ÷ 生成时间总和（非逐条平均），总产出在副行里
    const speedCard = screen.getByTestId("stat-speed");
    expect(speedCard).toHaveTextContent("52.0 tok/s");
    expect(speedCard).toHaveTextContent("总产出（含思考）94.1 tok/s");
    // 缓存命中率单列一格（P1 新增指标）
    expect(screen.getByTestId("stat-cache")).toHaveTextContent("40.0%");
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
    expect(screen.getByTestId("stat-credit")).toHaveTextContent("—");
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
    expect(screen.getByTestId("stat-credit")).toHaveTextContent("≈5.25");
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
        tps: null,
        tps_visible: null,
        tps_estimated: false,
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
    // 速度无样本：显示 —，并在 hint 里说明「为什么没有」而不是留白
    const speedCard = screen.getByTestId("stat-speed");
    expect(speedCard).toHaveTextContent("—");
    expect(speedCard).toHaveTextContent("没有可用的生成时间样本");
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

    // 默认最近 24 小时
    expect(screen.getByTestId("range-tabs-24h")).toHaveAttribute("aria-selected", "true");
    await userEvent.click(screen.getByTestId("range-tabs-7d"));
    // React 会重建 tablist 节点，断言必须每次现查，不能缓存元素引用
    await waitFor(() =>
      expect(screen.getByTestId("range-tabs-7d")).toHaveAttribute("aria-selected", "true"),
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

  it("新增派生指标可选：成功率 / 缓存命中率 / 单请求成本", async () => {
    const fetchSpy = mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/timeline": TIMELINE,
      "/api/stats/events": EVENTS_EMPTY,
    });
    renderPage(<StatsPage />, READER);
    await settle();
    fetchSpy.mockClear();

    for (const metric of ["success_rate", "cache_rate", "cost_per_request"]) {
      await userEvent.click(screen.getByTestId(`metric-tabs-${metric}`));
      await waitFor(() =>
        expect(fetchSpy.mock.calls.some(([url]) =>
          String(url).includes(`metric=${metric}`))).toBe(true),
      );
    }
  });

  it("主图默认画全部渠道合计，可切到单个渠道", async () => {
    mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/timeline": TIMELINE,
      "/api/stats/events": EVENTS_EMPTY,
    });
    renderPage(<StatsPage />, READER);
    await settle();

    // 渠道数 > 1 时出现切换条，默认选中「全部渠道」
    expect(screen.getByTestId("chart-scope-tabs-total"))
      .toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("usage-chart-legend")).toHaveTextContent("全部渠道");

    await userEvent.click(screen.getByTestId("chart-scope-tabs-trae"));
    await waitFor(() =>
      expect(screen.getByTestId("usage-chart-legend")).toHaveTextContent("TRAE"),
    );
  });

  it("延迟类指标额外展示分位图（P50 中位数 + P95 上界）", async () => {
    mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/timeline": TIMELINE,
      "/api/stats/percentile-timeline": PERCENTILE_TIMELINE,
      "/api/stats/events": EVENTS_EMPTY,
    });
    renderPage(<StatsPage />, READER);
    await settle();

    // 默认请求次数指标下不展示分位图
    expect(screen.queryByTestId("latency-chart")).toBeNull();

    await userEvent.click(screen.getByTestId("metric-tabs-latency"));
    await waitFor(() => expect(screen.getByTestId("latency-chart")).toBeInTheDocument());
    expect(screen.getByTestId("latency-chart-legend")).toHaveTextContent("P50 中位数");
    expect(screen.getByTestId("latency-chart-legend")).toHaveTextContent("P95 上界");
  });

  it("请求量指标下展示渠道构成（按请求数占比）", async () => {
    mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/timeline": TIMELINE,
      "/api/stats/events": EVENTS_EMPTY,
    });
    renderPage(<StatsPage />, READER);
    await settle();

    // 70 / (70+50) = 58.3%
    const share = screen.getByTestId("provider-share");
    expect(share).toHaveTextContent("TRAE");
    expect(share).toHaveTextContent("58.3%");

    // 换到非请求量指标后不展示（份额没有同一个分母）
    await userEvent.click(screen.getByTestId("metric-tabs-latency"));
    await waitFor(() => expect(screen.queryByTestId("provider-share")).toBeNull());
  });

  it("分组表点行下钻到该渠道的明细（键盘同样可触发）", async () => {
    const fetchSpy = mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": CREDENTIALS,
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/timeline": TIMELINE,
      "/api/stats/events": EVENTS_EMPTY,
    });
    renderPage(<StatsPage />, READER);
    await settle();
    fetchSpy.mockClear();

    // 点击：明细请求带 provider 过滤
    await userEvent.click(screen.getByLabelText("查看 trae 的请求明细"));
    await waitFor(() => expect(fetchSpy.mock.calls.some(([url]) =>
      String(url).includes("provider=trae"))).toBe(true));

    // 键盘：Enter 触发同一下钻（不只是鼠标可点）
    fetchSpy.mockClear();
    const cell = screen.getByLabelText("查看 codebuddy 的请求明细");
    cell.focus();
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(fetchSpy.mock.calls.some(([url]) =>
      String(url).includes("provider=codebuddy"))).toBe(true));
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

  it("请求明细：Token 合并列显示 输入/输出 · 命中率，未上报显示 —", async () => {
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
    expect(within(table).getByText("Token")).toBeInTheDocument();
    // 成功行：输入 120 / 输出 480 · 命中率 75.0%（命中 90 ÷ 输入 120）
    const okRow = within(table).getByText("glm-5.2").closest("tr")!;
    expect(okRow).toHaveTextContent("120 / 480 · 75.0%");
    // 失败行未上报缓存/输入 → 三项占位符
    const failedRow = within(table).getByText("额度耗尽").closest("tr")!;
    expect(failedRow).toHaveTextContent("— / — · —");
  });

  it("请求明细：速度合并列显示 正文/总产出，不可测的请求显示 —", async () => {
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
    // 单位以小字缀在列头（tok/s），行内只留数字
    expect(within(table).getByText("速度")).toBeInTheDocument();
    expect(within(table).getByText("tok/s")).toBeInTheDocument();
    // 成功行：后端算好的 正文 51.2 / 总产出 82.8 tok/s；悬停展开口径
    const okRow = within(table).getByText("glm-5.2").closest("tr")!;
    // 单位在列头（tok/s），行内只留数字——10 列的表每行重复单位要多占 45px
    expect(okRow).toHaveTextContent("51.2 / 82.8");
    await userEvent.hover(within(okRow).getByText("51.2 / 82.8"));
    expect(await screen.findByRole("tooltip")).toHaveTextContent("总产出（含思考）82.8 tok/s");
    // 失败行没有输出 → 生成窗口不可测，整格 —
    const failedRow = within(table).getByText("额度耗尽").closest("tr")!;
    expect(failedRow).toHaveTextContent("—");
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
            cost_usd: null, cost_cny: null, latency_ms: 400, ttfb_ms: 120,
            request_id: "req_a" },
          { rowid: 1, ts: BASE_TS, username: "root", provider: "codebuddy",
            model: "glm-5.2", credential_id: null, credential_name: null,
            ok: 1, error_type: null, input_tokens: 10, output_tokens: 5,
            cached_tokens: null, credit: 1.5, credit_estimated: 0,
            cost_usd: null, cost_cny: null, latency_ms: 300, ttfb_ms: 150,
            request_id: null },
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
    // 渠道 / 凭证已合并成一列：表头写明两列，凭证名进同一格的主行
    expect(within(table).getByText("渠道 / 凭证")).toBeInTheDocument();
    expect(within(table).queryByText("凭证")).not.toBeInTheDocument();
    expect(within(table).getByText("主账号")).toBeInTheDocument();
    expect(within(table).getByText("glm-5.2")).toBeInTheDocument();
    expect(within(table).getByText("成功")).toBeInTheDocument();
    expect(within(table).getByText("额度耗尽")).toBeInTheDocument();
    // 合并列：延迟「耗时 / 首字」，Token「输入 / 输出 · 命中率」
    const okRow = within(table).getByText("glm-5.2").closest("tr")!;
    expect(okRow).toHaveTextContent("120 / 480 · 75.0%");
    expect(okRow).toHaveTextContent("8.2 s / 2.4 s");
    const failRow = within(table).getByText("额度耗尽").closest("tr")!;
    expect(failRow).toHaveTextContent("460 ms / 120 ms");
    expect(screen.getByTestId("events-prev")).toBeDisabled();
    expect(screen.getByTestId("events-next")).toBeEnabled();
  });

  it("请求明细：时间拆成时刻 / 日期两行（时刻优先），渠道与凭证合并成一列（凭证优先）", async () => {
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
    const okRow = within(table).getByText("glm-5.2").closest("tr")!;

    // 时间格：第一行是具体时刻（正常色），第二行是日期（次要色）
    const timeCellEl = okRow.querySelector("[data-label='时间']")!;
    const [clockEl, dateEl] = Array.from(timeCellEl.children) as HTMLElement[];
    expect(clockEl.className).toContain("block");
    expect(clockEl.textContent).toMatch(/^\d{2}:\d{2}:\d{2}$/);
    expect(clockEl.className).not.toContain("muted-foreground");
    expect(dateEl.textContent).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(dateEl.className).toContain("text-muted-foreground");

    // 渠道 / 凭证：只有一列，凭证在主行、渠道（icon + 名）在副行，完整凭证 ID 落在 title
    const sourceCellEl = okRow.querySelector("[data-label='渠道 / 凭证']")!;
    const [credEl, providerEl] = Array.from(sourceCellEl.children) as HTMLElement[];
    expect(credEl.textContent).toBe("主账号");
    expect(credEl.className).not.toContain("muted-foreground");
    expect(providerEl.textContent).toContain("CodeBuddy");
    expect(providerEl.className).toContain("text-muted-foreground");
    expect(sourceCellEl).toHaveAttribute("title", "cred_abc123");
    expect(okRow.querySelector("[data-label='凭证']")).toBeNull();
    // 合并后仍可分别按渠道 / 凭证排序
    expect(screen.getByTestId("event-sort-source")).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("event-sort-source"));
    expect(await screen.findByTestId("event-sort-source-credential")).toBeInTheDocument();
  });

  it("请求明细的时间单元格带请求 ID 提示（老库历史行无提示）", async () => {
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
    // 首行（带 request_id）时间单元格带提示；第二行（request_id=null）不带
    const withId = within(table).getByTitle("请求 ID：req_page1");
    expect(withId).toBeInTheDocument();
    const rowsWithTitle = table.querySelectorAll("[title^='请求 ID：']");
    expect(rowsWithTitle).toHaveLength(1);
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

});
