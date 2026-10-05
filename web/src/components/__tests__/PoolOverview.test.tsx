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

describe("PoolOverview（原池仪表盘合并进凭证页的概览块）", () => {
  it("空池时全部计数为 0", () => {
    renderOverview([]);
    expect(screen.getByTestId("pool-overview")).toBeInTheDocument();
    expect(screen.getByTestId("health-known")).toHaveTextContent("已知剩余：0");
    expect(screen.getByTestId("health-unknown")).toHaveTextContent("未探测：0");
    expect(screen.getByTestId("health-noprobe")).toHaveTextContent("无探测：0");
    expect(screen.getByTestId("health-exhausted")).toHaveTextContent("已耗尽：0");
  });

  it("状态统计块与四态分布合并在同一容器内（不再各自成卡）", () => {
    renderOverview([credential({ id: "a", health: 62 })]);
    const overview = screen.getByTestId("pool-overview");
    // 单一容器：统计块与分布条同属一个卡片，用一条分隔线隔开
    expect(overview).toHaveTextContent("池概况");
    expect(overview).toHaveTextContent("健康度四态分布");
    // 统计块本身不再带 Card 边框/底色（否则视觉上又散成 6 张卡）
    for (const block of within(overview).getAllByText(/凭证总数|可用|冷却中|已禁用|额度耗尽|已暂停/)) {
      expect(block.closest("[data-slot='card']")).toHaveClass("border-0", "bg-transparent", "ring-0");
    }
  });

  it("区分健康度四态：已知 / 未探测 / 无探测 / 已耗尽", () => {
    renderOverview([
      credential({ id: "a", health: 62 }),
      credential({ id: "b", health: null, provider: "trae" }),
      credential({ id: "d", health: null, provider: "zen" }),
      credential({ id: "c", health: -1 }),
    ]);
    expect(screen.getByTestId("health-known")).toHaveTextContent("已知剩余：1");
    expect(screen.getByTestId("health-unknown")).toHaveTextContent("未探测：1");
    expect(screen.getByTestId("health-noprobe")).toHaveTextContent("无探测：1");
    expect(screen.getByTestId("health-exhausted")).toHaveTextContent("已耗尽：1");
    // 三种图例互不相同（图例在独立的 testid 节点里，直接断言其内容）
    expect(screen.getByTestId("health-unknown")).toHaveTextContent("未探测");
    expect(screen.getByTestId("health-noprobe")).toHaveTextContent("无探测");
    expect(screen.getByTestId("health-exhausted")).toHaveTextContent("已耗尽");
  });

  it("未探测失败不会被当成额度为 0", () => {
    renderOverview([credential({ health: null, provider: "trae" })]);
    expect(screen.getByTestId("health-exhausted")).toHaveTextContent("已耗尽：0");
    expect(screen.getByTestId("health-unknown")).toHaveTextContent("未探测：1");
  });
});
