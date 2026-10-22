import { screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { StatsPage } from "../StatsPage";
import { jsonResponse, mockFetch, renderPage, settle, userEvent } from "@/test-utils";
import {
  ADMIN,
  CREDENTIALS,
  EVENTS_EMPTY,
  EVENTS_PAGE1,
  EVENTS_PAGE2,
  MODEL_TIMELINE,
  MODELS,
  OVERVIEW,
  PROVIDERS,
  READER,
  USERS,
} from "@/test-utils/stats";

describe("StatsPage 分组与明细", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
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

    // 请求合并列下拉选「成功数」→ 后端 sort=ok_count
    await userEvent.click(screen.getByTestId("group-sort-requests"));
    await userEvent.click(screen.getByTestId("group-sort-requests-ok_count"));
    await waitFor(() => {
      const last = spy.mock.calls
        .map((call) => String(call[0]))
        .filter((url) => url.includes("/api/stats/by-provider"))
        .at(-1)!;
      expect(last).toContain("sort=ok_count");
    });
  });

  it("分组统计排序：点延迟列头按均值/P95 重新取数", async () => {
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

    const lastProviderCall = () => spy.mock.calls
      .map((call) => String(call[0]))
      .filter((url) => url.includes("/api/stats/by-provider"))
      .at(-1)!;

    // 均值列：合并列下拉选「平均耗时」→ SQL 表达式排序（hourly 的 sum ÷ ok_count）
    await userEvent.click(screen.getByTestId("group-sort-avg-latency"));
    await userEvent.click(screen.getByTestId("group-sort-avg-latency-avg_latency_ms"));
    await waitFor(() => expect(lastProviderCall()).toContain("sort=avg_latency_ms"));
    // 再点一次同列 → 切升序
    await userEvent.click(screen.getByTestId("group-sort-avg-latency"));
    await userEvent.click(screen.getByTestId("group-sort-avg-latency-avg_latency_ms"));
    await waitFor(() => expect(lastProviderCall()).toContain("order=asc"));

    // 分位列：LEFT JOIN 子查询算出的列
    await userEvent.click(screen.getByTestId("group-sort-p95-latency"));
    await userEvent.click(screen.getByTestId("group-sort-p95-latency-p95_latency_ms"));
    await waitFor(() => expect(lastProviderCall()).toContain("sort=p95_latency_ms"));
    await userEvent.click(screen.getByTestId("group-sort-p95-latency"));
    await userEvent.click(screen.getByTestId("group-sort-p95-latency-p95_ttfb_ms"));
    await waitFor(() => expect(lastProviderCall()).toContain("sort=p95_ttfb_ms"));
  });

  it("分组统计排序：Token 合并列下拉选子列即按其排序", async () => {
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

    const lastProviderCall = () => spy.mock.calls
      .map((call) => String(call[0]))
      .filter((url) => url.includes("/api/stats/by-provider"))
      .at(-1)!;

    for (const key of ["input_tokens", "output_tokens", "cached_tokens"]) {
      await userEvent.click(screen.getByTestId("group-sort-token"));
      await userEvent.click(screen.getByTestId(`group-sort-token-${key}`));
      await waitFor(() => expect(lastProviderCall()).toContain(`sort=${key}`));
    }

    // 速度合并列：正文 / 总产出两个分子各自是排序键
    for (const key of ["tps_visible", "tps"]) {
      await userEvent.click(screen.getByTestId("group-sort-speed"));
      await userEvent.click(screen.getByTestId(`group-sort-speed-${key}`));
      await waitFor(() => expect(lastProviderCall()).toContain(`sort=${key}`));
    }
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

  it("请求明细：Token/延迟合并列下拉选子列即按其排序", async () => {
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

    // Token 列下拉选「命中率」→ 后端 sort=cache_rate（派生列也能排）
    await userEvent.click(screen.getByTestId("event-sort-token"));
    await userEvent.click(screen.getByTestId("event-sort-token-cache_rate"));
    await waitFor(() => {
      const last = spy.mock.calls.map((call) => String(call[0]))
        .filter((url) => url.includes("/api/stats/events")).at(-1)!;
      expect(last).toContain("sort=cache_rate");
    });

    // 延迟列下拉选「首字」→ 后端 sort=ttfb_ms
    await userEvent.click(screen.getByTestId("event-sort-latency"));
    await userEvent.click(screen.getByTestId("event-sort-latency-ttfb_ms"));
    await waitFor(() => {
      const last = spy.mock.calls.map((call) => String(call[0]))
        .filter((url) => url.includes("/api/stats/events")).at(-1)!;
      expect(last).toContain("sort=ttfb_ms");
    });

    // 速度列下拉选「总产出速度」→ 后端 sort=tps（正文侧是 tps_visible）
    await userEvent.click(screen.getByTestId("event-sort-speed"));
    await userEvent.click(screen.getByTestId("event-sort-speed-tps"));
    await waitFor(() => {
      const last = spy.mock.calls.map((call) => String(call[0]))
        .filter((url) => url.includes("/api/stats/events")).at(-1)!;
      expect(last).toContain("sort=tps");
    });

    // 渠道/凭证合并列：合并后仍可分别按两个子列排序
    await userEvent.click(screen.getByTestId("event-sort-source"));
    await userEvent.click(screen.getByTestId("event-sort-source-credential"));
    await waitFor(() => {
      const last = spy.mock.calls.map((call) => String(call[0]))
        .filter((url) => url.includes("/api/stats/events")).at(-1)!;
      expect(last).toContain("sort=credential");
    });
    await userEvent.click(screen.getByTestId("event-sort-source"));
    await userEvent.click(screen.getByTestId("event-sort-source-provider"));
    await waitFor(() => {
      const last = spy.mock.calls.map((call) => String(call[0]))
        .filter((url) => url.includes("/api/stats/events")).at(-1)!;
      expect(last).toContain("sort=provider");
    });
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

  it("分组统计：各维度都给出成功率、命中缓存、成本与延迟", async () => {
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
    expect(row).toHaveTextContent("¥5.36");
    // Token 合并列：默认「输入 / 输出 · 命中率」，命中 token 落在悬停详情里
    expect(within(row).getByTestId("group-token")).toHaveTextContent("300 / 900");
    // 命中率列 = 命中 100 ÷ 输入 300 = 33.3%（命中 token 本身在悬停详情中）
    expect(within(row).getByTestId("group-cache-rate")).toHaveTextContent("33.3%");
    await userEvent.hover(within(row).getByTestId("group-cache-rate"));
    expect(await screen.findByRole("tooltip")).toHaveTextContent("命中 100");
    // 延迟四列：均值 4200/900 ms、P95 9500/2400 ms（都 ≥1s → 显示 s）
    expect(within(row).getByTestId("group-avg-latency")).toHaveTextContent("4.2 s");
    expect(within(row).getByTestId("group-avg-ttfb")).toHaveTextContent("900 ms");
    expect(within(row).getByTestId("group-p95-latency")).toHaveTextContent("9.5 s");
    expect(within(row).getByTestId("group-p95-ttfb")).toHaveTextContent("2.4 s");
    // 速度合并列：正文 58.0 / 总产出 102.0 tok/s（同分母，两个分子并成一列）
    expect(within(row).getByTestId("group-tps")).toHaveTextContent("58.0 / 102.0 tok/s");
    // kimi-k3 28/30 = 93.3%，推算成本标 ≈；未上报缓存 → 命中率显示 —
    row = within(screen.getByTestId("group-table")).getByText("kimi-k3").closest("tr")!;
    expect(row).toHaveTextContent("93.3%");
    expect(row).toHaveTextContent("≈¥2.68");
    expect(within(row).getByTestId("group-cache-rate")).toHaveTextContent("—");
    // 估算口径两个分子都前置 ≈（同一个 ≈ 覆盖整格）
    expect(within(row).getByTestId("group-tps")).toHaveTextContent("≈40.2 / 76.4 tok/s");

    // 按用户：alice 76/80 = 95.0%
    await userEvent.click(screen.getByTestId("group-tabs-user"));
    await settle();
    row = within(screen.getByTestId("group-table")).getByText("alice").closest("tr")!;
    expect(row).toHaveTextContent("95.0%");
    expect(row).toHaveTextContent("¥8.04");
    expect(within(row).getByTestId("group-p95-latency")).toHaveTextContent("31.9 s");

    // 按凭证：主账号行 68/70 = 97.1%
    await userEvent.click(screen.getByTestId("group-tabs-credential"));
    await settle();
    row = within(screen.getByTestId("group-table")).getByText("主账号").closest("tr")!;
    expect(row).toHaveTextContent("97.1%");
    expect(row).toHaveTextContent("≈¥3.35");
    expect(within(row).getByTestId("group-avg-latency")).toHaveTextContent("8.2 s");
    expect(within(row).getByTestId("group-tps")).toHaveTextContent("61.0 / 88.0 tok/s");
    // 无成功样本的凭证行：延迟四列都是 —（不拿 0 冒充有数据）
    row = within(screen.getByTestId("group-table")).getByText("cred_zzz9998").closest("tr")!;
    expect(within(row).getByTestId("group-avg-latency")).toHaveTextContent("—");
    expect(within(row).getByTestId("group-avg-ttfb")).toHaveTextContent("—");
    expect(within(row).getByTestId("group-p95-latency")).toHaveTextContent("—");
    expect(within(row).getByTestId("group-p95-ttfb")).toHaveTextContent("—");
    expect(within(row).getByTestId("group-tps")).toHaveTextContent("—");
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

  it("分组统计：分位档可切换，列头标签/单元格/排序键随所选档变化", async () => {
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

    // 默认 P95：既有 testId 与列头文案不变
    expect(screen.getByTestId("percentile-tabs-p95"))
      .toHaveAttribute("aria-selected", "true");
    const row = screen.getByTestId("group-row-trae");
    expect(within(row).getByTestId("group-p95-latency")).toHaveTextContent("22.2 s");
    expect(screen.getByTestId("group-sort-p95-latency")).toHaveTextContent("P95 延迟");

    // 切 P50：单元格换成 P50 值（6200ms → 6.2 s），排序菜单键也跟着换
    await userEvent.click(screen.getByTestId("percentile-tabs-p50"));
    await waitFor(() => expect(screen.getByTestId("group-sort-p50-latency")).toBeInTheDocument());
    expect(screen.getByTestId("group-sort-p50-latency")).toHaveTextContent("P50 延迟");
    const p50Row = screen.getByTestId("group-row-trae");
    expect(within(p50Row).getByTestId("group-p50-latency")).toHaveTextContent("6.2 s");
    expect(within(p50Row).getByTestId("group-p50-ttfb")).toHaveTextContent("1.8 s");
    expect(within(p50Row).queryByTestId("group-p95-latency")).not.toBeInTheDocument();

    // P50 列头下拉按 p50 键排序（后端白名单已含）
    await userEvent.click(screen.getByTestId("group-sort-p50-latency"));
    await userEvent.click(screen.getByTestId("group-sort-p50-latency-p50_ttfb_ms"));
    await waitFor(() => {
      const last = spy.mock.calls.map((call) => String(call[0]))
        .filter((url) => url.includes("/api/stats/by-provider")).at(-1)!;
      expect(last).toContain("sort=p50_ttfb_ms");
    });

    // P99 同样可用（四档后端一次算好，切换不发新请求）
    const callsBefore = spy.mock.calls.length;
    await userEvent.click(screen.getByTestId("percentile-tabs-p99"));
    await waitFor(() => expect(screen.getByTestId("group-sort-p99-latency")).toBeInTheDocument());
    expect(spy.mock.calls.length).toBe(callsBefore);
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
