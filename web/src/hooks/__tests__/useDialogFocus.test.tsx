import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { useDialogFocus } from "../useDialogFocus";

/** 最小对话框：容器上挂 hook，内含两个可聚焦元素与一个容器外按钮。 */
function Dialog({
  onDismiss,
  active = true,
}: {
  onDismiss?: () => void;
  active?: boolean;
}) {
  const ref = useDialogFocus<HTMLDivElement>(onDismiss, active);
  return (
    <div>
      <button>外部按钮</button>
      <div ref={ref} role="dialog" aria-label="测试对话框">
        <button>第一个</button>
        <button>第二个</button>
      </div>
    </div>
  );
}

describe("useDialogFocus", () => {
  it("打开时把焦点移入第一个可聚焦元素", () => {
    render(<Dialog />);
    expect(screen.getByText("第一个")).toHaveFocus();
  });

  it("Esc 触发 onDismiss", async () => {
    const onDismiss = vi.fn();
    render(<Dialog onDismiss={onDismiss} />);
    await userEvent.keyboard("{Escape}");
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("没有 onDismiss 时 Esc 不抛错也不关闭", async () => {
    render(<Dialog />);
    await userEvent.keyboard("{Escape}");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("Tab 在对话框内循环，不逃逸到外部", async () => {
    render(<Dialog />);
    // 初始焦点在「第一个」
    await userEvent.tab();
    expect(screen.getByText("第二个")).toHaveFocus();
    // 末尾 Tab → 回到首个
    await userEvent.tab();
    expect(screen.getByText("第一个")).toHaveFocus();
    // 首部 Shift+Tab → 到末尾
    await userEvent.tab({ shift: true });
    expect(screen.getByText("第二个")).toHaveFocus();
  });

  it("卸载时把焦点还给打开它的元素", () => {
    const outside = document.createElement("button");
    document.body.appendChild(outside);
    outside.focus();

    const { unmount } = render(<Dialog />);
    expect(screen.getByText("第一个")).toHaveFocus();
    unmount();
    expect(outside).toHaveFocus();
    outside.remove();
  });

  it("active=false 时不接管焦点（常驻挂载的按需显示）", () => {
    const outside = document.createElement("button");
    document.body.appendChild(outside);
    outside.focus();

    render(<Dialog active={false} />);
    expect(outside).toHaveFocus();
    outside.remove();
  });

  it("打开期间锁定 body 滚动，关闭后还原", () => {
    document.body.style.overflow = "auto";
    const { unmount } = render(<Dialog />);
    expect(document.body.style.overflow).toBe("hidden");
    unmount();
    expect(document.body.style.overflow).toBe("auto");
    document.body.style.overflow = "";
  });
});