import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DashboardPage } from "../DashboardPage";
import { makeCredential, mockFetch, renderPage, settle } from "./helpers";

describe("DashboardPage", () => {
  it("空池时全部计数为 0", async () => {
    mockFetch({ "/api/credentials": { credentials: [], viewer: "root", is_admin: true } });
    renderPage(<DashboardPage />);
    await settle();

    expect(screen.getByTestId("dashboard")).toBeInTheDocument();
    expect(screen.getByTestId("no-cooling")).toBeInTheDocument();
    expect(screen.getByTestId("health-known")).toHaveTextContent("已知剩余：0");
    expect(screen.getByTestId("health-unknown")).toHaveTextContent("未探测到额度：0");
    expect(screen.getByTestId("health-exhausted")).toHaveTextContent("已耗尽：0");
  });

  it("区分健康度三态：已知 / 未探测 / 已耗尽", async () => {
    mockFetch({
      "/api/credentials": {
        credentials: [
          makeCredential({ id: "a", health: 62 }),
          makeCredential({ id: "b", health: null }),
          makeCredential({ id: "c", health: -1 }),
        ],
        viewer: "root",
        is_admin: true,
      },
    });
    renderPage(<DashboardPage />);
    await settle();

    expect(screen.getByTestId("health-known")).toHaveTextContent("已知剩余：1");
    expect(screen.getByTestId("health-unknown")).toHaveTextContent("未探测到额度：1");
    expect(screen.getByTestId("health-exhausted")).toHaveTextContent("已耗尽：1");
    // 未探测必须与已耗尽文案不同
    expect(screen.getByText("未探测到额度")).toBeInTheDocument();
    expect(screen.getByText("已耗尽")).toBeInTheDocument();
  });

  it("未探测失败不会被当成额度为 0", async () => {
    mockFetch({
      "/api/credentials": {
        credentials: [makeCredential({ health: null })],
        viewer: "root",
        is_admin: true,
      },
    });
    renderPage(<DashboardPage />);
    await settle();
    expect(screen.queryByText("已耗尽")).not.toBeInTheDocument();
    expect(screen.getAllByText("未探测到额度").length).toBeGreaterThan(0);
  });

  it("冷却中的账号显示剩余时间与原因", async () => {
    const future = Math.floor(Date.now() / 1000) + 3600;
    mockFetch({
      "/api/credentials": {
        credentials: [
          makeCredential({
            id: "cooling",
            nickname: "冷却号",
            cooling_until: future,
            disabled_reason: "plan 权益不足",
          }),
        ],
        viewer: "root",
        is_admin: true,
      },
    });
    renderPage(<DashboardPage />);
    await settle();

    const list = screen.getByTestId("cooling-list");
    expect(list).toHaveTextContent("冷却号");
    expect(list).toHaveTextContent("剩余");
    expect(list).toHaveTextContent("plan 权益不足");
  });

  it("凭证池单列表展示，行内标注所属渠道", async () => {
    mockFetch({
      "/api/credentials": {
        credentials: [
          makeCredential({ id: "t1", provider: "trae" }),
          makeCredential({ id: "c1", provider: "codebuddy" }),
        ],
        viewer: "root",
        is_admin: true,
      },
    });
    renderPage(<DashboardPage />);
    await settle();

    // 不再按渠道分组成两个列表，而是单一列表 + 行内渠道标注
    expect(screen.getByTestId("credential-list")).toBeInTheDocument();
    expect(screen.queryByTestId("provider-group-trae")).not.toBeInTheDocument();
    expect(screen.getByTestId("credential-t1")).toHaveAttribute("data-provider", "trae");
    expect(screen.getByTestId("credential-c1")).toHaveAttribute("data-provider", "codebuddy");
  });

  it("区分周期额度与单调余额的语义", async () => {
    mockFetch({
      "/api/credentials": {
        credentials: [
          makeCredential({ id: "cb", provider: "codebuddy", quota_cycle_end: 1_800_000_000 }),
          makeCredential({ id: "tr", provider: "trae", quota_cycle_end: null }),
        ],
        viewer: "root",
        is_admin: true,
      },
    });
    renderPage(<DashboardPage />);
    await settle();

    expect(screen.getByText(/本周期剩余/)).toBeInTheDocument();
    expect(screen.getAllByText(/账户剩余（单调递减）/).length).toBeGreaterThan(0);
  });
});
