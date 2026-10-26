import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { StatsPage } from "../StatsPage";
import { mockFetch, renderPage, settle, userEvent } from "@/test-utils";
import {
  ADMIN,
  CREDENTIALS,
  EVENTS_EMPTY,
  MODEL_TIMELINE,
  MODELS,
  OVERVIEW,
  PROVIDERS,
  USERS,
} from "@/test-utils/stats";

function statsRoutes() {
  return {
    "/api/stats/overview": OVERVIEW,
    "/api/stats/by-provider": PROVIDERS,
    "/api/stats/by-model": MODELS,
    "/api/stats/by-user": USERS,
    "/api/stats/by-credential": CREDENTIALS,
    "/api/stats/model-timeline": MODEL_TIMELINE,
    "/api/stats/events": EVENTS_EMPTY,
  };
}

/** 取最后一次命中某路径的请求 URL。 */
function lastCall(spy: ReturnType<typeof mockFetch>, path: string) {
  return spy.mock.calls
    .map(([url]) => String(url))
    .filter((url) => url.includes(path))
    .at(-1)!;
}

describe("StatsPage 自定义时间范围", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("切到自定义：预填最近 24 小时并按本地时区下发 since/until", async () => {
    const spy = mockFetch(statsRoutes());
    renderPage(<StatsPage />, ADMIN);
    await settle();

    await userEvent.click(screen.getByTestId("range-tabs-custom"));
    await settle();

    // 两个输入框被预填（非空）
    expect((screen.getByTestId("custom-from") as HTMLInputElement).value).not.toBe("");
    expect((screen.getByTestId("custom-to") as HTMLInputElement).value).not.toBe("");

    // 请求同时带 since 与 until（半开区间）
    const url = lastCall(spy, "/api/stats/overview");
    expect(url).toContain("since=");
    expect(url).toContain("until=");
    const params = new URLSearchParams(url.split("?")[1]);
    expect(Number(params.get("until"))).toBeGreaterThan(Number(params.get("since")));
  });

  it("手动改起止：只带对应的端点参数", async () => {
    const spy = mockFetch(statsRoutes());
    renderPage(<StatsPage />, ADMIN);
    await settle();

    await userEvent.click(screen.getByTestId("range-tabs-custom"));
    await settle();

    fireEvent.change(screen.getByTestId("custom-from"), { target: { value: "2026-01-02T03:04" } });
    fireEvent.change(screen.getByTestId("custom-to"), { target: { value: "2026-01-03T03:04" } });
    await settle();
    await settle();

    const params = new URLSearchParams(lastCall(spy, "/api/stats/overview").split("?")[1]);
    expect(params.get("since")).toBe(String(Math.floor(new Date("2026-01-02T03:04").getTime() / 1000)));
    expect(params.get("until")).toBe(String(Math.floor(new Date("2026-01-03T03:04").getTime() / 1000)));
  });

  it("起点晚于终点：提示非法并暂停取数（不发无时间窗的全量查询）", async () => {
    const spy = mockFetch(statsRoutes());
    renderPage(<StatsPage />, ADMIN);
    await settle();

    await userEvent.click(screen.getByTestId("range-tabs-custom"));
    await settle();

    // 先把起点改成一个**合法**窗口（终点还是预填的当前时刻），此时取数正常
    fireEvent.change(screen.getByTestId("custom-from"), { target: { value: "2026-01-03T03:04" } });
    await settle();
    await settle();
    const before = spy.mock.calls.filter(([url]) =>
      String(url).includes("/api/stats/overview")).length;

    // 再把终点改到起点之前 → 窗口非法
    fireEvent.change(screen.getByTestId("custom-to"), { target: { value: "2026-01-02T03:04" } });
    await settle();
    await settle();

    expect(screen.getByTestId("custom-range-invalid")).toBeInTheDocument();
    // 非法窗口**不再发请求**：此前 effectiveSince/Until 变 undefined，请求照发
    // 且不带时间参数——等于静默查全量，还会在逐字编辑输入框时反复触发全表扫。
    const after = spy.mock.calls.filter(([url]) =>
      String(url).includes("/api/stats/overview")).length;
    expect(after).toBe(before);
  });

  it("导出 CSV 带上当前自定义窗口", async () => {
    const spy = mockFetch({
      ...statsRoutes(),
      "/api/stats/events": (url: string) =>
        url.includes("format=csv")
          ? new Response("a,b\r\n", {
              status: 200,
              headers: { "X-Export-Row-Count": "1", "X-Export-Truncated": "0" },
            })
          : EVENTS_EMPTY,
    });
    vi.stubGlobal("URL", Object.assign(URL, {
      createObjectURL: vi.fn(() => "blob:mock"),
      revokeObjectURL: vi.fn(),
    }));
    renderPage(<StatsPage />, ADMIN);
    await settle();

    await userEvent.click(screen.getByTestId("range-tabs-custom"));
    await settle();
    await userEvent.click(screen.getByTestId("events-export"));
    await waitFor(() => {
      const csv = spy.mock.calls
        .map(([url]) => String(url))
        .filter((url) => url.includes("format=csv"))
        .at(-1);
      expect(csv).toBeDefined();
      expect(csv).toContain("since=");
      expect(csv).toContain("until=");
    });
  });
});
