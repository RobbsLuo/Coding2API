import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ThemeToggle } from "../ThemeToggle";

describe("ThemeToggle", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    document.documentElement.classList.remove("dark");
    localStorage.clear();
  });

  it("切换时改 <html>.dark 并写入 localStorage", async () => {
    render(<ThemeToggle />);
    await userEvent.click(screen.getByRole("button", { name: "切换主题" }));

    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(localStorage.getItem("theme")).toBe("dark");
  });

  it("存储被禁用（Safari 隐私模式）时主题仍切换，不抛 SecurityError", async () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    render(<ThemeToggle />);

    await userEvent.click(screen.getByRole("button", { name: "切换主题" }));

    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });
});
