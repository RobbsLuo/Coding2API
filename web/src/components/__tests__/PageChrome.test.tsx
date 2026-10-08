import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EmptyState, Metric } from "../../ui";
import { PageHeader } from "../PageHeader";
import { PageSkeleton } from "../PageSkeleton";

describe("PageSkeleton", () => {
  it("默认卡片形态：带稳定 testid 与读屏「载入中…」", () => {
    render(<PageSkeleton />);
    expect(screen.getByTestId("page-skeleton")).toBeInTheDocument();
    // 既有测试以该文本判断加载态；骨架必须保留 sr-only 文案
    expect(screen.getByText("载入中…")).toBeInTheDocument();
  });

  it("table / chart 形态可切换行数与结构", () => {
    const { rerender } = render(<PageSkeleton variant="chart" rows={2} />);
    expect(screen.getByTestId("page-skeleton")).toBeInTheDocument();
    rerender(<PageSkeleton variant="table" rows={8} />);
    expect(screen.getByTestId("page-skeleton")).toBeInTheDocument();
  });
});

describe("PageHeader", () => {
  it("渲染 eyebrow、标题、说明与右侧操作槽", () => {
    render(
      <PageHeader
        eyebrow="控制台"
        title="凭证管理"
        description="一句话说明"
        icon={<span data-testid="header-icon" />}
        actions={<button type="button">新建</button>}
      />,
    );
    expect(screen.getByRole("heading", { name: "凭证管理" })).toBeInTheDocument();
    expect(screen.getByText("控制台")).toBeInTheDocument();
    expect(screen.getByText("一句话说明")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "新建" })).toBeInTheDocument();
  });

  it("最简用法：只有标题", () => {
    render(<PageHeader title="审计日志" />);
    expect(screen.getByRole("heading", { name: "审计日志" })).toBeInTheDocument();
  });
});

describe("EmptyState / Metric", () => {
  it("EmptyState 渲染标题、描述与操作", () => {
    render(
      <EmptyState
        title="还没有凭证"
        description="添加一个吧"
        action={<button type="button">添加</button>}
        data-testid="empty"
      />,
    );
    expect(screen.getByTestId("empty")).toHaveTextContent("还没有凭证");
    expect(screen.getByRole("button", { name: "添加" })).toBeInTheDocument();
  });

  it("Metric 渲染标签、数值与提示", () => {
    render(<Metric label="请求数" value="10,867" hint="成功率 99.7%" />);
    expect(screen.getByText("请求数")).toBeInTheDocument();
    expect(screen.getByText("10,867")).toBeInTheDocument();
    expect(screen.getByText("成功率 99.7%")).toBeInTheDocument();
  });
});
