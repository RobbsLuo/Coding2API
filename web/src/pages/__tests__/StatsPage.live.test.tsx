import { screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { StatsPage } from "../StatsPage";
import { mockFetch, MockEventSource, renderPage, settle, userEvent } from "@/test-utils";
import {
  ADMIN,
  EVENTS_PAGE1,
  MODEL_TIMELINE,
  MODELS,
  OVERVIEW,
  PROVIDERS,
  CREDENTIALS,
  USERS,
} from "@/test-utils/stats";

/** 实时流（SSE）开关：建连、推事件、断线提示、关闭即断开。 */
describe("StatsPage 实时明细", () => {
  const routes = {
    "/api/stats/overview": OVERVIEW,
    "/api/stats/by-provider": PROVIDERS,
    "/api/stats/by-model": MODELS,
    "/api/stats/by-user": USERS,
    "/api/stats/by-credential": CREDENTIALS,
    "/api/stats/model-timeline": MODEL_TIMELINE,
    "/api/stats/events": EVENTS_PAGE1,
  };

  beforeEach(() => {
    MockEventSource.reset();
  });

  it("默认不建连，勾上「实时」后订阅并展示新行", async () => {
    mockFetch(routes);
    renderPage(<StatsPage />, ADMIN);
    await settle();
    // 默认关闭：不占用订阅槽位（后端最多 8 个）
    expect(MockEventSource.instances).toHaveLength(0);
    expect(screen.queryByTestId("events-live")).not.toBeInTheDocument();

    await userEvent.click(screen.getByTestId("events-live-toggle"));
    const source = MockEventSource.last();
    expect(source.url).toContain("/api/stats/events/stream");
    expect(screen.getByTestId("events-live")).toHaveTextContent("已连接，等待新的请求");

    // emit 只能在 waitFor 外调一次：回调会重试，放里面会重复推同一行
    source.emit({ ...EVENTS_PAGE1.events[0], rowid: 999, model: "live-model" });
    await waitFor(() => {
      expect(screen.getByTestId("events-live-table")).toHaveTextContent("live-model");
    });
    expect(screen.getAllByTestId("live-row")).toHaveLength(1);
    // 实时行与分页明细表同列：速度按广播行自带的 tps / tps_visible 渲染
    const liveTable = screen.getByTestId("events-live-table");
    expect(liveTable).toHaveTextContent("速度");
    expect(liveTable).toHaveTextContent("51.2 / 82.8");
    // 时间两行（时刻优先、日期在下）、渠道/凭证合并列：实时表与分页表同一套渲染
    const liveCell = screen.getAllByTestId("live-row")[0]
      .querySelector("[data-label='时间']")!;
    expect(liveCell.children).toHaveLength(2);
    expect(liveCell.children[0].textContent).toMatch(/^\d{2}:\d{2}:\d{2}$/);
    expect(liveCell.children[1].textContent).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    const liveRow = screen.getAllByTestId("live-row")[0];
    expect(liveRow.querySelector("[data-label='凭证']")).toBeNull();
    const liveSource = liveRow.querySelector("[data-label='渠道 / 凭证']")!;
    expect(liveSource.children[0].textContent).toBe("主账号");
    expect(liveSource.textContent).toContain("CodeBuddy");
  });

  it("实时行按新→旧排列，坏帧被丢弃不影响后续", async () => {
    mockFetch(routes);
    renderPage(<StatsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByTestId("events-live-toggle"));
    const source = MockEventSource.last();

    source.emit({ ...EVENTS_PAGE1.events[0], rowid: 1, model: "older" });
    source.emitRaw("{ half json");
    source.emit({ ...EVENTS_PAGE1.events[0], rowid: 2, model: "newer" });
    await waitFor(() => {
      expect(screen.getAllByTestId("live-row")).toHaveLength(2);
    });
    const rows = screen.getAllByTestId("live-row");
    expect(rows[0]).toHaveTextContent("newer");
    expect(rows[1]).toHaveTextContent("older");
  });

  it("携带钻取过滤：重建连接带上 provider/model", async () => {
    mockFetch(routes);
    renderPage(<StatsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByTestId("events-live-toggle"));
    const first = MockEventSource.last();
    expect(first.url).not.toContain("provider=");

    // 点分组表行 → 钻取 → 连接按新过滤重建（EventSource 无法改查询串）
    await userEvent.click(screen.getAllByRole("button", { name: /查看 .* 的请求明细/ })[0]);
    await waitFor(() => {
      expect(MockEventSource.instances.length).toBeGreaterThan(1);
    });
    expect(first.closed).toBe(true);
    expect(MockEventSource.last().url).toContain("provider=");
  });

  it("断线时提示重连/断开，关掉开关即断开连接", async () => {
    mockFetch(routes);
    renderPage(<StatsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByTestId("events-live-toggle"));
    const source = MockEventSource.last();

    // readyState 仍为 OPEN：浏览器会自动重连，页面保持「已连接」不吓人
    await waitFor(() => {
      source.fail(1);
      expect(screen.getByTestId("events-live")).toHaveTextContent("已连接，等待新的请求");
    });

    // readyState=CLOSED：浏览器放弃重连（401/429 等），给明确提示
    await waitFor(() => {
      source.fail(MockEventSource.CLOSED);
      expect(screen.getByTestId("events-live")).toHaveTextContent("实时连接已断开");
    });

    // 关掉开关：close + 清空已收到的行
    await userEvent.click(screen.getByTestId("events-live-toggle"));
    expect(source.closed).toBe(true);
    expect(screen.queryByTestId("events-live")).not.toBeInTheDocument();
  });

  it("环境不支持 EventSource 时按断开处理（不假装已连上）", async () => {
    mockFetch(routes);
    vi.stubGlobal("EventSource", undefined);
    renderPage(<StatsPage />, ADMIN);
    await settle();
    await userEvent.click(screen.getByTestId("events-live-toggle"));
    expect(screen.getByTestId("events-live")).toHaveTextContent("实时连接已断开");
  });
});
