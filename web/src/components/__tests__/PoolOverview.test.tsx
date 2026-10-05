import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PoolOverview } from "../PoolOverview";
import { makeCredential, renderPage } from "../../pages/__tests__/helpers";
import type { Credential } from "../../api/types";

function renderOverview(credentials: Credential[]) {
  renderPage(<PoolOverview credentials={credentials} now={Date.now() / 1000} />);
}

function credential(overrides: Record<string, unknown> = {}): Credential {
  return makeCredential(overrides) as Credential;
}

describe("PoolOverview（池概况：一条分布条 + 一行四态摘要）", () => {
  it("空池时给出明确提示，可用量为 0", () => {
    renderOverview([]);
    expect(screen.getByTestId("pool-overview")).toHaveTextContent("池里还没有凭证");
    expect(screen.getByTestId("health-known")).toHaveTextContent("可用");
    expect(screen.getByTestId("health-known")).toHaveTextContent("0");
    // 零计数的三态不占位（没有信息量）
    expect(screen.queryByTestId("health-unknown")).not.toBeInTheDocument();
    expect(screen.queryByTestId("health-noprobe")).not.toBeInTheDocument();
    expect(screen.queryByTestId("health-exhausted")).not.toBeInTheDocument();
    expect(screen.queryByTestId("pool-blocked")).not.toBeInTheDocument();
  });

  it("四态各计数正确，可用量带总数做分母", () => {
    renderOverview([
      credential({ id: "a", health: 62 }),
      credential({ id: "b", health: 80 }),
      credential({ id: "c", health: null, provider: "trae" }),
      credential({ id: "d", health: null, provider: "zen" }),
      credential({ id: "e", health: -1 }),
    ]);
    expect(screen.getByTestId("health-known")).toHaveTextContent("可用");
    expect(screen.getByTestId("health-known")).toHaveTextContent("2");
    expect(screen.getByTestId("health-known")).toHaveTextContent("/ 5");
    expect(screen.getByTestId("health-unknown")).toHaveTextContent("未探测");
    expect(screen.getByTestId("health-unknown")).toHaveTextContent("1");
    expect(screen.getByTestId("health-noprobe")).toHaveTextContent("无探测");
    expect(screen.getByTestId("health-noprobe")).toHaveTextContent("1");
    expect(screen.getByTestId("health-exhausted")).toHaveTextContent("已耗尽");
    expect(screen.getByTestId("health-exhausted")).toHaveTextContent("1");
  });

  it("未探测失败不会被当成额度为 0（不出现「已耗尽」）", () => {
    renderOverview([credential({ health: null, provider: "trae" })]);
    expect(screen.queryByTestId("health-exhausted")).not.toBeInTheDocument();
    expect(screen.getByTestId("health-unknown")).toHaveTextContent("1");
  });

  it("冷却中 / 已禁用 / 已暂停合并成一行不可用摘要", () => {
    const future = Math.floor(Date.now() / 1000) + 3600;
    renderOverview([
      credential({ id: "cool", cooling_until: future }),
      credential({ id: "dis", disabled: 1 }),
      credential({ id: "off", enabled: 0 }),
      credential({ id: "ok1" }),
    ]);
    const blocked = screen.getByTestId("pool-blocked");
    expect(within(blocked).getByTestId("blocked-cooling")).toHaveTextContent("冷却中 1");
    expect(within(blocked).getByTestId("blocked-disabled")).toHaveTextContent("已禁用 1");
    expect(within(blocked).getByTestId("blocked-off")).toHaveTextContent("已暂停 1");
    expect(blocked.children).toHaveLength(3);
  });

  it("只有冷却中时摘要只出现那一项", () => {
    const future = Math.floor(Date.now() / 1000) + 600;
    renderOverview([credential({ id: "cool", cooling_until: future })]);
    const blocked = screen.getByTestId("pool-blocked");
    expect(blocked.children).toHaveLength(1);
    expect(within(blocked).getByTestId("blocked-cooling")).toBeInTheDocument();
    expect(within(blocked).queryByTestId("blocked-disabled")).not.toBeInTheDocument();
  });

  it("不再单列统计格，也不再重复「额度耗尽」", () => {
    renderOverview([credential({ id: "c", health: -1 })]);
    const overview = screen.getByTestId("pool-overview");
    // 数字只在图例行里出现一次；没有独立的 Card 统计格
    expect(overview.querySelectorAll("[data-slot='card']")).toHaveLength(0);
    expect(screen.queryByText("凭证总数")).not.toBeInTheDocument();
    expect(screen.queryByText("额度耗尽")).not.toBeInTheDocument();
    // 四态语义说明段已移除（由表格列头 ColumnHint 承载）
    expect(overview).not.toHaveTextContent("调度器优先选");
  });
});
