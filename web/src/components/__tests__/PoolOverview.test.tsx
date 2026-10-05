import { screen } from "@testing-library/react";
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
