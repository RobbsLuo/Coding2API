import { render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CompressPanel } from "../CompressPanel";
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

describe("CompressPanel", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("同屏展示触发次数、省下 token、命中率与开关状态", async () => {
    const spy = mockFetch({ "/api/stats/compression": COMPRESSION });
    renderStandalone(
      <CompressPanel stats={OVERVIEW} isOperator since={1700000000} until={1700086400} />,
    );

    expect(await screen.findByText("触发次数")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("省下 token（估算）")).toBeInTheDocument();
    expect(screen.getByText("1万")).toBeInTheDocument();
    expect(screen.getByText("同期缓存命中率")).toBeInTheDocument();
    // OVERVIEW.cached_tokens=400 / input_tokens=1000 → 40.0%
    expect(screen.getByText("40.0%")).toBeInTheDocument();
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
        <CompressPanel stats={OVERVIEW} isOperator since={1000} />
      </QueryClientProvider>,
    );
    await screen.findByText("触发次数");
    rerender(
      <QueryClientProvider client={client}>
        <CompressPanel stats={OVERVIEW} isOperator since={2000} />
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
    renderStandalone(<CompressPanel stats={OVERVIEW} isOperator />);

    expect(await screen.findByTestId("compress-panel-empty")).toHaveTextContent(
      "所选时间窗内压缩未触发",
    );
    expect(await screen.findByText(/上下文裁剪 开 · tool 输出 关/)).toBeInTheDocument();
  });

  it("自定义范围非法（暂停取数）时不发请求", () => {
    const spy = mockFetch({ "/api/stats/compression": COMPRESSION });
    renderStandalone(<CompressPanel stats={undefined} isOperator rangeReady={false} />);

    expect(screen.getByTestId("compress-panel")).toBeInTheDocument();
    expect(spy).not.toHaveBeenCalled();
  });

  it("viewer 不渲染面板、也不发请求", () => {
    const spy = mockFetch({ "/api/stats/compression": COMPRESSION });
    renderStandalone(<CompressPanel stats={OVERVIEW} isOperator={false} />);

    expect(screen.queryByTestId("compress-panel")).not.toBeInTheDocument();
    expect(spy).not.toHaveBeenCalled();
  });

  it("端点失败时降级为说明，不拖垮页面", async () => {
    mockFetch({
      "/api/stats/compression": () => new Response("nope", { status: 503 }),
    });
    renderStandalone(<CompressPanel stats={undefined} isOperator />);

    expect(await screen.findByTestId("compress-panel-error")).toHaveTextContent(
      "压缩读数不可用",
    );
  });
});