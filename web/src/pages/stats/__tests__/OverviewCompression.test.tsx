import { render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { StatsOverviewCards } from "../StatsOverviewCards";
import { makeClient, mockFetch, renderStandalone } from "@/test-utils";
import { OVERVIEW } from "@/test-utils/stats";

const COMPRESSION = {
  stats: {
    triggers: 12,
    tool_triggers: 9,
    context_trims: 3,
    chars_saved: 40000,
    tokens_saved: 10000,
    unchanged: 7,
  },
  context_compress_enabled: true,
  tool_output_compress_enabled: true,
};

/** 概览面板里的压缩读数（operator 才渲染）：与缓存命中率同屏、同一时间窗。 */
describe("概览面板 · 压缩读数", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("同屏展示触发次数、省下 token、命中率与开关状态", async () => {
    const spy = mockFetch({ "/api/stats/compression": COMPRESSION });
    renderStandalone(
      <StatsOverviewCards
        overviewFailed={false}
        stats={OVERVIEW}
        isOperator
        since={1700000000}
        until={1700086400}
      />,
    );

    expect(await screen.findByText("压缩触发次数")).toBeInTheDocument();
    expect(await screen.findByText("12")).toBeInTheDocument();
    expect(screen.getByText("省下 token（估算）")).toBeInTheDocument();
    expect(screen.getByText("1万")).toBeInTheDocument();
    expect(screen.getByText("同期缓存命中率")).toBeInTheDocument();
    // OVERVIEW.cached_tokens=400 / input_tokens=1000 → 40.0%；总览的缓存格同值，
    // 同屏出现两次正是「压缩没有把命中率拉低」的证据。
    expect(screen.getAllByText("40.0%")).toHaveLength(2);
    expect(screen.getByText(/上下文裁剪 开 · tool 输出 开/)).toBeInTheDocument();
    expect(screen.getByText(/tool 输出 9 · 上下文裁剪 3/)).toBeInTheDocument();
    // 时间窗必须随请求下发，否则后端回的是进程终身累计，与同屏命中率口径错位
    expect(spy).toHaveBeenCalledWith(
      "/api/stats/compression?since=1700000000&until=1700086400",
      expect.anything(),
    );
  });

  it("时间窗变化会按新窗口重新取数", async () => {
    const spy = mockFetch({ "/api/stats/compression": COMPRESSION });
    const client = makeClient();
    const { rerender } = render(
      <QueryClientProvider client={client}>
        <StatsOverviewCards overviewFailed={false} stats={OVERVIEW} isOperator since={1000} />
      </QueryClientProvider>,
    );
    await screen.findByText("压缩触发次数");
    rerender(
      <QueryClientProvider client={client}>
        <StatsOverviewCards overviewFailed={false} stats={OVERVIEW} isOperator since={2000} />
      </QueryClientProvider>,
    );

    await waitFor(() =>
      expect(spy).toHaveBeenCalledWith(
        "/api/stats/compression?since=2000",
        expect.anything(),
      ),
    );
  });

  it("空态：所选时间窗内压缩未触发且无原地不变记录", async () => {
    mockFetch({
      "/api/stats/compression": {
        stats: {
          triggers: 0, tool_triggers: 0, context_trims: 0,
          chars_saved: 0, tokens_saved: 0, unchanged: 0,
        },
        context_compress_enabled: true,
        tool_output_compress_enabled: false,
      },
    });
    renderStandalone(
      <StatsOverviewCards overviewFailed={false} stats={OVERVIEW} isOperator />,
    );

    expect(await screen.findByTestId("compress-panel-empty")).toHaveTextContent(
      "所选时间窗内压缩未触发",
    );
    expect(await screen.findByText(/上下文裁剪 开 · tool 输出 关/)).toBeInTheDocument();
  });

  it("自定义范围非法（暂停取数）时不发请求", () => {
    const spy = mockFetch({ "/api/stats/compression": COMPRESSION });
    renderStandalone(
      <StatsOverviewCards
        overviewFailed={false}
        stats={undefined}
        isOperator
        rangeReady={false}
      />,
    );

    expect(screen.getByTestId("compress-panel")).toBeInTheDocument();
    expect(spy).not.toHaveBeenCalled();
  });

  it("viewer 不渲染压缩读数、也不发请求", () => {
    const spy = mockFetch({ "/api/stats/compression": COMPRESSION });
    renderStandalone(
      <StatsOverviewCards overviewFailed={false} stats={OVERVIEW} isOperator={false} />,
    );

    expect(screen.queryByTestId("compress-panel")).not.toBeInTheDocument();
    expect(screen.queryByText("压缩触发次数")).not.toBeInTheDocument();
    expect(spy).not.toHaveBeenCalled();
  });

  it("端点失败时降级为说明，不拖垮页面", async () => {
    mockFetch({
      "/api/stats/compression": () => new Response("nope", { status: 503 }),
    });
    renderStandalone(
      <StatsOverviewCards overviewFailed={false} stats={undefined} isOperator />,
    );

    expect(await screen.findByTestId("compress-panel-error")).toHaveTextContent(
      "压缩读数不可用",
    );
  });
});