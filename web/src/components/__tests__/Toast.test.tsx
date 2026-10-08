import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ToastViewport, useToasts } from "../Toast";
import type { ToastItem } from "../Toast";

function item(overrides: Partial<ToastItem> = {}): ToastItem {
  return { id: 1, tone: "ok", testId: "toast-1", content: "探测成功", ...overrides };
}

/** 用 hook 把 push/dismiss 暴露成按钮，验证同类去重与关闭逻辑。 */
function Harness() {
  const { toasts, push, dismiss } = useToasts();
  return (
    <div>
      <button onClick={() => push({ tone: "ok", testId: "same", content: "第一条" })}>push1</button>
      <button onClick={() => push({ tone: "ok", testId: "same", content: "第二条" })}>push2</button>
      <button onClick={() => push({ tone: "danger", testId: "other", content: "错误" })}>push3</button>
      <ToastViewport toasts={toasts} onDismiss={dismiss} />
    </div>
  );
}

describe("ToastViewport", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("成功提示用 status 播报，失败提示用 alert 强播报", () => {
    render(
      <ToastViewport
        toasts={[item({ tone: "ok" }), item({ id: 2, tone: "danger", testId: "toast-2" })]}
        onDismiss={() => {}}
      />,
    );
    expect(screen.getByTestId("toast-1")).toHaveAttribute("role", "status");
    expect(screen.getByTestId("toast-2")).toHaveAttribute("role", "alert");
  });

  it("到时自动关闭", async () => {
    vi.useFakeTimers();
    const onDismiss = vi.fn();
    render(
      <ToastViewport toasts={[item({ duration: 3000 })]} onDismiss={onDismiss} />,
    );
    expect(onDismiss).not.toHaveBeenCalled();
    await act(async () => {
      vi.advanceTimersByTime(3000);
    });
    expect(onDismiss).toHaveBeenCalledWith(1);
  });

  it("duration<=0 时保留，不自动关闭", async () => {
    vi.useFakeTimers();
    const onDismiss = vi.fn();
    render(<ToastViewport toasts={[item({ duration: 0 })]} onDismiss={onDismiss} />);
    await act(async () => {
      vi.advanceTimersByTime(60_000);
    });
    expect(onDismiss).not.toHaveBeenCalled();
  });

  it("点关闭按钮立即关闭", async () => {
    const onDismiss = vi.fn();
    render(<ToastViewport toasts={[item()]} onDismiss={onDismiss} />);
    await userEvent.click(screen.getByRole("button", { name: "关闭提示" }));
    expect(onDismiss).toHaveBeenCalledWith(1);
  });
});

describe("useToasts", () => {
  it("同 testId 的提示只保留最新一条（连续探测不堆屏）", async () => {
    render(<Harness />);
    await userEvent.click(screen.getByText("push1"));
    await userEvent.click(screen.getByText("push2"));
    expect(screen.queryByText("第一条")).not.toBeInTheDocument();
    expect(screen.getByText("第二条")).toBeInTheDocument();
  });

  it("不同 testId 的提示并存", async () => {
    render(<Harness />);
    await userEvent.click(screen.getByText("push1"));
    await userEvent.click(screen.getByText("push3"));
    expect(screen.getByTestId("same")).toHaveTextContent("第一条");
    expect(screen.getByTestId("other")).toHaveTextContent("错误");
  });

  it("关闭后移除对应提示", async () => {
    render(<Harness />);
    await userEvent.click(screen.getByText("push1"));
    await userEvent.click(screen.getAllByRole("button", { name: "关闭提示" })[0]);
    expect(screen.queryByTestId("same")).not.toBeInTheDocument();
  });
});