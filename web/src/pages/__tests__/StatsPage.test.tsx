import { screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { StatsPage } from "../StatsPage";
import { mockFetch, renderPage, settle, userEvent } from "./helpers";

const ADMIN = { username: "root", is_admin: true } as const;
const READER = { username: "alice", is_admin: false } as const;

const OVERVIEW = {
  requests: 120,
  ok_count: 114,
  success_rate: 0.95,
  input_tokens: 1000,
  output_tokens: 2000,
  reasoning_tokens: 300,
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

describe("StatsPage", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("渲染总览指标与按上游分组", async () => {
    mockFetch({ "/api/stats/overview": OVERVIEW, "/api/stats/by-provider": PROVIDERS });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    expect(screen.getByTestId("stats-page")).toHaveTextContent("120");
    expect(screen.getByText("95.0%")).toBeInTheDocument();
    expect(screen.getByTestId("provider-table")).toHaveTextContent("CodeBuddy");
    expect(screen.getByTestId("provider-table")).toHaveTextContent("TRAE");
  });

  it("credit 为 null 时显示占位符而非 0", async () => {
    mockFetch({
      "/api/stats/overview": { ...OVERVIEW, credit: null },
      "/api/stats/by-provider": PROVIDERS,
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    // TRAE 那一行的 credit 为 null
    const row = screen.getByText("TRAE").closest("tr")!;
    expect(row).toHaveTextContent("—");
    // 总览未探测到 credit
    expect(screen.getByText("Credit 消耗").parentElement).toHaveTextContent("—");
  });

  it("成功率缺失时显示占位符", async () => {
    mockFetch({
      "/api/stats/overview": {
        ...OVERVIEW,
        requests: 0,
        ok_count: 0,
        success_rate: null,
        avg_latency_ms: null,
        avg_ttfb_ms: null,
      },
      "/api/stats/by-provider": { providers: [] },
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();
    expect(screen.getByText("成功率").parentElement).toHaveTextContent("—");
    expect(screen.getByTestId("no-provider-stats")).toBeInTheDocument();
  });

  it("非管理员看不到用户名筛选器", async () => {
    mockFetch({ "/api/stats/overview": OVERVIEW, "/api/stats/by-provider": PROVIDERS });
    renderPage(<StatsPage />, READER);
    await settle();
    expect(screen.queryByTestId("username-filter")).not.toBeInTheDocument();
  });

  it("管理员可按用户名筛选并触发新查询", async () => {
    const fetchSpy = mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    expect(screen.getByTestId("username-filter")).toBeInTheDocument();
    await userEvent.type(screen.getByTestId("username-filter"), "bob");

    await waitFor(() =>
      expect(fetchSpy.mock.calls.some(([url]) => String(url).includes("username=bob"))).toBe(true),
    );
  });

  it("切换时间范围触发带 since 的新查询", async () => {
    const fetchSpy = mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
    });
    renderPage(<StatsPage />, READER);
    await settle();

    await userEvent.selectOptions(screen.getByTestId("range-select"), "24h");
    await waitFor(() =>
      expect(fetchSpy.mock.calls.some(([url]) => String(url).includes("since="))).toBe(true),
    );
  });

  it("选择「全部」时不带 since 参数", async () => {
    const fetchSpy = mockFetch({
      "/api/stats/overview": OVERVIEW,
      "/api/stats/by-provider": PROVIDERS,
    });
    renderPage(<StatsPage />, READER);
    await settle();
    fetchSpy.mockClear();

    await userEvent.selectOptions(screen.getByTestId("range-select"), "all");
    await waitFor(() => expect(fetchSpy).toHaveBeenCalled());
    expect(fetchSpy.mock.calls.every(([url]) => !String(url).includes("since="))).toBe(true);
  });
});
