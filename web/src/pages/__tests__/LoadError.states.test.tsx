import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AlertsPage } from "../AlertsPage";
import { ApiKeysPage } from "../ApiKeysPage";
import { AuditPage } from "../AuditPage";
import { CredentialsPage } from "../CredentialsPage";
import { SettingsPage } from "../SettingsPage";
import { StatsPage } from "../StatsPage";
import { UsersPage } from "../UsersPage";
import { mockFetch, renderPage, settle } from "@/test-utils";
import { ADMIN, EVENTS_EMPTY, MODEL_TIMELINE, MODELS, PROVIDERS, USERS } from "@/test-utils/stats";
import { tasksBody } from "@/test-utils/settings";

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

  it("API Key 页：列表请求失败 → 错误态，不显示「还没有 API Key」", async () => {
    mockFetch({
      "/api/api-keys": () =>
        new Response(JSON.stringify({ error: { message: "boom" } }), {
          status: 500,
          headers: { "Content-Type": "application/json" },
        }),
      "/api/playground/models": { object: "list", data: [] },
    });
    renderPage(<ApiKeysPage />, ADMIN);
    await settle();

    expect(screen.getByTestId("api-keys-error-state")).toBeInTheDocument();
    // 关键：故障不能被讲成「你还没建 Key」
    expect(screen.queryByTestId("no-keys")).toBeNull();
  });

  it("用户页：列表请求失败 → 错误态，不显示「还没有用户」", async () => {
    mockFetch({
      "/api/users": () =>
        new Response(JSON.stringify({ error: { message: "boom" } }), {
          status: 500,
          headers: { "Content-Type": "application/json" },
        }),
    });
    renderPage(<UsersPage />, ADMIN);
    await settle();

    expect(screen.getByTestId("users-error-state")).toBeInTheDocument();
    expect(screen.queryByTestId("no-users")).toBeNull();
  });

  it("审计页：列表请求失败 → 错误态，不显示「没有符合条件的记录」", async () => {
    mockFetch({
      "/api/audit": () =>
        new Response(JSON.stringify({ error: { message: "boom" } }), {
          status: 500,
          headers: { "Content-Type": "application/json" },
        }),
    });
    renderPage(<AuditPage />, ADMIN);
    await settle();

    expect(screen.getByTestId("audit-error-state")).toBeInTheDocument();
    // 关键：缺证据（故障）和没有记录必须能区分开
    expect(screen.queryByTestId("no-audit")).toBeNull();
  });

  it("设置页：运行时配置请求失败 → 错误态，不显示「没有可热更配置」", async () => {
    mockFetch({
      "/api/settings": () =>
        new Response(JSON.stringify({ error: { message: "boom" } }), {
          status: 500,
          headers: { "Content-Type": "application/json" },
        }),
      "/api/tasks": tasksBody(),
    });
    renderPage(<SettingsPage />, ADMIN);
    await settle();

    expect(screen.getByTestId("settings-error-state")).toBeInTheDocument();
    expect(screen.queryByTestId("no-settings")).toBeNull();
  });
});
