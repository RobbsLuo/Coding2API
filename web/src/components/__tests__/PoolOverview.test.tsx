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

describe("PoolOverview（池概况：健康度四态为主轴 + 不可用原因摘要）", () => {
  it("空池时全部计数为 0，且不渲染不可用摘要", () => {
    renderOverview([]);
    expect(screen.getByTestId("pool-overview")).toBeInTheDocument();
    expect(screen.getByTestId("pool-total")).toHaveTextContent("凭证总数");
    expect(screen.getByTestId("pool-total")).toHaveTextContent("0");
    for (const [id, label] of [["health-known", "可用"], ["health-unknown", "未探测"],
                               ["health-noprobe", "无探测"], ["health-exhausted", "已耗尽"]]) {
      expect(screen.getByTestId(id)).toHaveTextContent(label);
      expect(screen.getByTestId(id)).toHaveTextContent("0");
    }
    // 池里没有不可用的号时，摘要行整块不渲染
    expect(screen.queryByTestId("pool-blocked")).not.toBeInTheDocument();
  });

  it("区分健康度四态：已知 / 未探测 / 无探测 / 已耗尽", () => {
    renderOverview([
      credential({ id: "a", health: 62 }),
      credential({ id: "b", health: null, provider: "trae" }),
      credential({ id: "d", health: null, provider: "zen" }),
      credential({ id: "c", health: -1 }),
    ]);
    for (const [id, count] of [["health-known", "1"], ["health-unknown", "1"],
                               ["health-noprobe", "1"], ["health-exhausted", "1"]]) {
      expect(screen.getByTestId(id)).toHaveTextContent(count);
    }
    // 三种图例互不相同（各自独立 testid 节点，直接断言其标签）
    expect(screen.getByTestId("health-unknown")).toHaveTextContent("未探测");
    expect(screen.getByTestId("health-noprobe")).toHaveTextContent("无探测");
    expect(screen.getByTestId("health-exhausted")).toHaveTextContent("已耗尽");
  });

  it("未探测失败不会被当成额度为 0", () => {
    renderOverview([credential({ health: null, provider: "trae" })]);
    expect(screen.getByTestId("health-exhausted")).toHaveTextContent("0");
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
    // 全部不可用状态只是一行摘要，不再各占一个统计格
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

  it("健康度已耗尽不再另设「额度耗尽」统计格（避免与四态重复计数）", () => {
    renderOverview([credential({ id: "c", health: -1 })]);
    expect(screen.getByTestId("health-exhausted")).toHaveTextContent("已耗尽");
    expect(screen.queryByText("额度耗尽")).not.toBeInTheDocument();
  });

  it("统计格子并进同一卡片（不带 Card 边框/底色）", () => {
    renderOverview([credential({ id: "a", health: 62 })]);
    const overview = screen.getByTestId("pool-overview");
    // 单一容器：总数 + 四态 + 说明同属一张卡
    expect(overview).toHaveTextContent("池概况");
    for (const id of ["pool-total", "health-known", "health-unknown",
                      "health-noprobe", "health-exhausted"]) {
      const card = within(overview).getByTestId(id).querySelector("[data-slot='card']");
      expect(card).toHaveClass("border-0", "bg-transparent", "ring-0");
    }
  });
});
