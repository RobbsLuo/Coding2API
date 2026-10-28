import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ErrorBoundary } from "../ErrorBoundary";

function Boom(): never {
  throw new Error("render exploded");
}

describe("ErrorBoundary", () => {
  let consoleError: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    // React 会把 caught error 再打一遍 console.error；断言前静音，避免测试输出噪音
    consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    consoleError.mockRestore();
  });

  it("正常子树原样渲染", () => {
    render(<ErrorBoundary><p>hello</p></ErrorBoundary>);
    expect(screen.getByText("hello")).toBeInTheDocument();
    expect(screen.queryByTestId("error-boundary")).not.toBeInTheDocument();
  });

  it("子树抛错显示失败面板与错误信息", () => {
    render(<ErrorBoundary><Boom /></ErrorBoundary>);
    expect(screen.getByTestId("error-boundary")).toBeInTheDocument();
    expect(screen.getByTestId("error-boundary-message")).toHaveTextContent("render exploded");
    expect(screen.getByRole("link", { name: "回到首页" })).toHaveAttribute("href", "/");
    // 恢复入口存在；重载动作本身在 jsdom 里不可观测，点击行为交给浏览器
    expect(screen.getByTestId("error-boundary-reload")).toBeInTheDocument();
  });
});
