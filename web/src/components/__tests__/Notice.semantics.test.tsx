import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Notice } from "../../ui";

/**
 * Notice 的无障碍语义与视觉规范（MASTER §5「Notice」行）。
 *
 * 两条纪律：
 * 1. **只有 danger 用 `role="alert"`**：读屏会把 `role=alert` 当「立刻打断
 *    播报」。中性信息（muted / ok / accent）也用 alert，等于把「凭证池是空的」
 *    当成错误念给用户。
 * 2. **左侧语义色条**：整圈边框看不出语气，色条一眼可辨。
 */
describe("Notice 语义", () => {
  it("danger 用 role=alert（错误要立刻播报）", () => {
    render(<Notice tone="danger">出错了</Notice>);
    expect(screen.getByRole("alert")).toHaveTextContent("出错了");
  });

  it("其余语气用 role=status（不打断播报）", () => {
    for (const tone of ["muted", "ok", "warn", "accent"] as const) {
      const { unmount } = render(<Notice tone={tone}>提示</Notice>);
      expect(screen.queryByRole("alert")).toBeNull();
      expect(screen.getByRole("status")).toHaveTextContent("提示");
      unmount();
    }
  });

  it("默认语气是 muted（非错误）", () => {
    render(<Notice>提示</Notice>);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("左侧有语义色条（border-l-4 + 语气色）", () => {
    const { container } = render(<Notice tone="danger">出错了</Notice>);
    const alert = container.querySelector("[role='alert']")!;
    const classes = alert.className;
    expect(classes).toContain("border-l-4");
    expect(classes).toContain("border-l-destructive");
  });
});
