import { screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { StatsPage } from "../StatsPage";
import { mockFetch, MockEventSource, renderPage, settle, userEvent } from "@/test-utils";
import {
  ADMIN,
  CREDENTIALS,
  ERRORS,
  EVENTS_EMPTY,
  MODEL_TIMELINE,
  MODELS,
  OVERVIEW,
  PROVIDERS,
  READER,
  USERS,
} from "@/test-utils/stats";

function statsRoutes(errors: unknown = ERRORS) {
  return {
    "/api/stats/overview": OVERVIEW,
    "/api/stats/by-provider": PROVIDERS,
    "/api/stats/by-model": MODELS,
    "/api/stats/by-user": USERS,
    "/api/stats/by-credential": CREDENTIALS,
    "/api/stats/model-timeline": MODEL_TIMELINE,
    "/api/stats/events": EVENTS_EMPTY,
    "/api/stats/errors": errors,
  };
}

describe("StatsPage 失败归因", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("按渠道/模型/错误类型展示失败数与最近时刻，计数条按最大值归一", async () => {
    mockFetch(statsRoutes());
    renderPage(<StatsPage />, ADMIN);
    await settle();

    const table = screen.getByTestId("error-table");
    // 三列归因 + 计数 + 最近时刻
    const rateLimitRow = within(table).getByTestId("error-row-trae-glm-5.2-rate_limit");
    expect(rateLimitRow).toHaveTextContent("TRAE");
    expect(rateLimitRow).toHaveTextContent("glm-5.2");
    expect(rateLimitRow).toHaveTextContent("额度耗尽");
    expect(rateLimitRow).toHaveTextContent("12");
    // 计数条：12 是最大值 → 100% 宽
    const bar = rateLimitRow.querySelector<HTMLElement>("span[style]");
    expect(bar?.style.width).toBe("100%");
    // 4/12 ≈ 33%
    const upstreamRow = within(table).getByTestId("error-row-codebuddy-kimi-k3-upstream_error");
    expect(upstreamRow.querySelector<HTMLElement>("span[style]")?.style.width).toBe("33%");
    // 未上报错误类型 → 「失败」（不把 NULL 原样透传）
    const unknownRow = within(table).getByTestId("error-row-trae-deepseek-v4-unknown");
    expect(unknownRow).toHaveTextContent("失败");
    // 未救回 = failures − rescued：12−4=8 / 4−4=0 / 2−0=2
    expect(screen.getByTestId("error-unrescued-trae-glm-5.2-rate_limit"))
      .toHaveTextContent("8");
    expect(screen.getByTestId("error-unrescued-codebuddy-kimi-k3-upstream_error"))
      .toHaveTextContent("0");
    expect(screen.getByTestId("error-unrescued-trae-deepseek-v4-unknown"))
      .toHaveTextContent("2");
  });

  it("点归因行 → 明细按 provider+model+ok=0 重新取数并显示过滤标签", async () => {
    const spy = mockFetch(statsRoutes());
    renderPage(<StatsPage />, ADMIN);
    await settle();

    await userEvent.click(screen.getByTestId("error-row-trae-glm-5.2-rate_limit"));
    await waitFor(() => {
      const last = spy.mock.calls.map((call) => String(call[0]))
        .filter((url) => url.includes("/api/stats/events")).at(-1)!;
      expect(last).toContain("provider=trae");
      expect(last).toContain("model=glm-5.2");
      expect(last).toContain("ok=0");
    });
    const chips = screen.getByTestId("drill-filters");
    expect(chips).toHaveTextContent("渠道：TRAE");
    expect(chips).toHaveTextContent("模型：glm-5.2");
    expect(chips).toHaveTextContent("只看失败");

    // 清除后回到无过滤（不再带 ok）
    await userEvent.click(screen.getByTestId("drill-clear"));
    await waitFor(() => expect(screen.queryByTestId("drill-filters")).not.toBeInTheDocument());
    const last = spy.mock.calls.map((call) => String(call[0]))
      .filter((url) => url.includes("/api/stats/events")).at(-1)!;
    expect(last).not.toContain("ok=");
  });

  it("键盘回车同样触发下钻（行是 role=button）", async () => {
    const spy = mockFetch(statsRoutes());
    renderPage(<StatsPage />, ADMIN);
    await settle();

    screen.getByTestId("error-row-codebuddy-kimi-k3-upstream_error").focus();
    await userEvent.keyboard("{Enter}");
    await waitFor(() => {
      const last = spy.mock.calls.map((call) => String(call[0]))
        .filter((url) => url.includes("/api/stats/events")).at(-1)!;
      expect(last).toContain("ok=0");
    });
  });

  it("没有失败时整张卡不渲染（不给空态占版面）", async () => {
    mockFetch(statsRoutes({ errors: [] }));
    renderPage(<StatsPage />, ADMIN);
    await settle();

    // 「零失败」不必占一张卡：空态 + 标题栏 + 口径说明约 239px，
    // 把真正的数据推到更下面。无失败时整块隐藏。
    expect(screen.queryByTestId("error-panel")).not.toBeInTheDocument();
    expect(screen.queryByTestId("error-table")).not.toBeInTheDocument();
  });

  it("加载失败给出可诊断提示", async () => {
    mockFetch({
      ...statsRoutes(),
      "/api/stats/errors": () => new Response("", { status: 500 }),
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    expect(await screen.findByTestId("errors-error")).toHaveTextContent("失败归因加载失败");
  });

  it("下钻后开启实时流：连接地址带上 ok=0（与明细同口径）", async () => {
    MockEventSource.reset();
    mockFetch(statsRoutes());
    renderPage(<StatsPage />, ADMIN);
    await settle();

    await userEvent.click(screen.getByTestId("error-row-trae-glm-5.2-rate_limit"));
    await userEvent.click(screen.getByTestId("events-live-toggle"));
    expect(MockEventSource.last().url).toContain("ok=0");
    expect(MockEventSource.last().url).toContain("provider=trae");
  });

  it("只读用户不渲染失败归因区块，也不发该请求", async () => {
    const spy = mockFetch(statsRoutes());
    renderPage(<StatsPage />, READER);
    await settle();

    expect(screen.queryByTestId("error-panel")).not.toBeInTheDocument();
    expect(spy.mock.calls.some(([url]) => String(url).includes("/api/stats/errors")))
      .toBe(false);
  });
});
