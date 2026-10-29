import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { SortMenuHead } from "../SortMenuHead";
import { Table, TableHead, TableRow } from "@/components/ui/table";

/**
 * 合并列列头：右对齐时箭头在最左，但「列名 → 单位」的阅读顺序不能倒。
 *
 * 早期实现用 `flex-row-reverse` 整行反转，结果表头渲染成「tok/s 速度」——
 * 单位跑到列名前面，读起来像另一个列名。这里按 DOM 顺序断言真实阅读顺序。
 */
function renderHead(props: Partial<Parameters<typeof SortMenuHead>[0]> = {}) {
  render(
    <Table>
      <TableHead>
        <TableRow>
          <SortMenuHead
            label="速度"
            unit="tok/s"
            options={[{ key: "tps", label: "总产出速度" }]}
            activeKey="tps"
            direction="desc"
            onToggle={vi.fn()}
            align="right"
            testId="head"
            {...props}
          />
        </TableRow>
      </TableHead>
    </Table>,
  );
  return screen.getByTestId("head");
}

describe("SortMenuHead", () => {
  it("右对齐：箭头在列名之前，列名仍在单位之前", () => {
    const button = renderHead();
    // 文本顺序 = 视觉顺序（不靠 flex 反转），单位不会跑到列名前
    expect(button.textContent).toBe("速度tok/s· 总产出速度");
    // 箭头是第一个子元素（右对齐列把图标放左），单位在列名之后
    const first = button.firstElementChild as HTMLElement;
    expect(first.tagName.toLowerCase()).toBe("svg");
    const unit = within(button).getByText("tok/s");
    const label = within(button).getByText("速度");
    expect(label.compareDocumentPosition(unit) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("左对齐：箭头仍在列名之后", () => {
    const button = renderHead({ align: "left", unit: undefined });
    const last = button.lastElementChild as HTMLElement;
    expect(last.tagName.toLowerCase()).toBe("svg");
  });

  it("点列头弹子列菜单，选中项回传排序键", async () => {
    const onToggle = vi.fn();
    const button = renderHead({ onToggle });
    await userEvent.click(button);
    await userEvent.click(await screen.findByTestId("head-tps"));
    expect(onToggle).toHaveBeenCalledWith("tps");
  });
});
