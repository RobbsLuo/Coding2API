import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AlertsPage } from "../AlertsPage";
import { CredentialsPage } from "../CredentialsPage";
import { StatsPage } from "../StatsPage";
import { mockFetch, renderPage, settle } from "@/test-utils";
import { ADMIN, EVENTS_EMPTY, MODEL_TIMELINE, MODELS, PROVIDERS, USERS } from "@/test-utils/stats";

/**
 * 加载失败 ≠ 空数据。
 *
 * 三个页面此前都只判 `isLoading` / `length === 0`，请求失败时被渲染成
 * 「没有用量 / 没有凭证 / 没有告警」——用户会把故障读成「系统健康」，
 * 故障被完全吞掉（统计页尤其危险：管理员以为没人在用上游）。
 */
describe("加载失败显式报错（不伪装成空数据）", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("统计页：总览请求失败 → 显式错误提示", async () => {
    mockFetch({
      "/api/stats/overview": () =>
        new Response(JSON.stringify({ error: { message: "boom" } }), {
          status: 500,
          headers: { "Content-Type": "application/json" },
        }),
      "/api/stats/by-provider": PROVIDERS,
      "/api/stats/by-model": MODELS,
      "/api/stats/by-user": USERS,
      "/api/stats/by-credential": { credentials: [] },
      "/api/stats/model-timeline": MODEL_TIMELINE,
      "/api/stats/events": EVENTS_EMPTY,
    });
    renderPage(<StatsPage />, ADMIN);
    await settle();

    expect(screen.getByTestId("stats-overview-error")).toBeInTheDocument();
  });

  it("凭证页：列表请求失败 → 错误态，不显示「还没有凭证」", async () => {
    mockFetch({
      "/api/credentials": () =>
        new Response(JSON.stringify({ error: { message: "boom" } }), {
          status: 500,
          headers: { "Content-Type": "application/json" },
        }),
    });
    renderPage(<CredentialsPage />, ADMIN);
    await settle();

    expect(screen.getByTestId("credentials-load-error")).toBeInTheDocument();
    expect(screen.getByTestId("credentials-error-state")).toBeInTheDocument();
    // 关键：不能显示「还没有凭证」——那是「池子真的空的」的说法
    expect(screen.queryByTestId("no-credentials")).toBeNull();
  });

  it("告警页：列表请求失败 → 错误态，不显示「暂无告警记录」", async () => {
    mockFetch({
      "/api/alerts": () =>
        new Response(JSON.stringify({ error: { message: "boom" } }), {
          status: 500,
          headers: { "Content-Type": "application/json" },
        }),
    });
    renderPage(<AlertsPage />, ADMIN);
    await settle();

    expect(screen.getByTestId("alerts-error-state")).toBeInTheDocument();
    // 关键：不能显示「暂无告警记录…一切正常时保持空白」
    expect(screen.queryByTestId("no-alerts")).toBeNull();
  });
});
