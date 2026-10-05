import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AlertsPage } from "../AlertsPage";
import { renderPage, settle } from "./helpers";

function stubFetch(body: unknown, status = 200) {
  const spy = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", spy);
  return spy;
}

const ALERT = {
  id: "alert_1", ts: 1_700_000_000, rule: "pool_empty", severity: "critical",
  scope: "pool", message: "凭证池可用数为 0", detail: "{}", delivered: 1,
  delivery_error: null,
};

describe("AlertsPage", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("渲染告警行：规则标签、级别、推送状态", async () => {
    stubFetch({ alerts: [
      ALERT,
      { ...ALERT, id: "alert_2", rule: "task_failed", severity: "warning",
        scope: "growth", delivered: 0, delivery_error: null },
      { ...ALERT, id: "alert_3", rule: "error_rate", severity: "warning",
        scope: "pool", delivered: 0, delivery_error: "500" },
    ] });
    renderPage(<AlertsPage />, { username: "root", is_admin: true });
    await settle();

    expect(screen.getByTestId("alerts-table")).toBeInTheDocument();
    expect(screen.getByTestId("alert-row-alert_1")).toHaveTextContent("凭证池耗尽");
    expect(screen.getByTestId("alert-row-alert_1")).toHaveTextContent("严重");
    expect(screen.getByTestId("alert-row-alert_1")).toHaveTextContent("已推送");
    expect(screen.getByTestId("alert-row-alert_2")).toHaveTextContent("后台任务连续失败");
    expect(screen.getByTestId("alert-row-alert_2")).toHaveTextContent("站内");
    expect(screen.getByTestId("alert-row-alert_3")).toHaveTextContent("推送失败");
  });

  it("未知规则回落到原始 key，不崩", async () => {
    stubFetch({ alerts: [{ ...ALERT, id: "alert_x", rule: "brand_new_rule" }] });
    renderPage(<AlertsPage />, { username: "root", is_admin: true });
    await settle();
    expect(screen.getByTestId("alert-row-alert_x")).toHaveTextContent("brand_new_rule");
  });

  it("无记录时显示占位", async () => {
    stubFetch({ alerts: [] });
    renderPage(<AlertsPage />, { username: "root", is_admin: true });
    await settle();
    expect(screen.getByTestId("no-alerts")).toBeInTheDocument();
  });
});