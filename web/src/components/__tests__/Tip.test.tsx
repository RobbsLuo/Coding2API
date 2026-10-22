import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ColumnHint, LongTextTip, Tip } from "../Tip";
import { userEvent } from "@/test-utils";

/** 浮层表面：短提示用反色气泡（带箭头），多行清单用卡片表面（无箭头）。 */
describe("Tip 浮层表面", () => {
  it("Tip 是反色气泡，带箭头", async () => {
    render(<Tip content="一句话说明"><button type="button">目标</button></Tip>);
    await userEvent.hover(screen.getByText("目标"));

    const tip = await screen.findByRole("tooltip");
    expect(tip).toHaveAttribute("data-surface", "ink");
    expect(tip).toHaveTextContent("一句话说明");
    expect(tip.querySelector("[data-slot='tooltip-arrow']")).toBeInTheDocument();
  });

  it("LongTextTip 是卡片表面，不出箭头（箭头会盖住描边留缺口）", async () => {
    render(
      <LongTextTip content={<div>多行清单</div>}>
        <span>触发元素</span>
      </LongTextTip>,
    );
    await userEvent.hover(screen.getByText("触发元素"));

    const tip = await screen.findByRole("tooltip");
    expect(tip).toHaveAttribute("data-surface", "panel");
    expect(tip).toHaveTextContent("多行清单");
    expect(tip.querySelector("[data-slot='tooltip-arrow']")).not.toBeInTheDocument();
    // 多行内容左对齐：居中会让每行起点都不同，读不成列
    expect(tip.className).toContain("text-left");
  });

  it("ColumnHint 沿用反色气泡", async () => {
    const { container } = render(<ColumnHint text="该列语义" />);
    const trigger = container.querySelector("[data-slot='tooltip-trigger']");
    expect(trigger).not.toBeNull();
    await userEvent.hover(trigger as Element);

    const tip = await screen.findByRole("tooltip");
    expect(tip).toHaveAttribute("data-surface", "ink");
    expect(tip).toHaveTextContent("该列语义");
  });
});
