import { afterEach, describe, expect, it, vi } from "vitest";
import { copyText } from "../clipboard";

describe("copyText", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("有 Clipboard API 时走 writeText", async () => {
    const writeText = vi.fn(async () => undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });

    await expect(copyText("hello")).resolves.toBe(true);
    expect(writeText).toHaveBeenCalledWith("hello");
  });

  it("非安全上下文（clipboard 为 undefined）回落到 execCommand", async () => {
    vi.stubGlobal("navigator", {});
    const execCommand = vi.fn(() => true);
    Object.defineProperty(document, "execCommand", { value: execCommand, configurable: true });

    await expect(copyText("局域网部署")).resolves.toBe(true);
    expect(execCommand).toHaveBeenCalledWith("copy");
    // 兜底用的临时 textarea 不能留在 DOM 里
    expect(document.querySelectorAll("textarea")).toHaveLength(0);
  });

  it("writeText 抛错（权限被拒）时也走兜底", async () => {
    const writeText = vi.fn(async () => { throw new Error("denied"); });
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    const execCommand = vi.fn(() => true);
    Object.defineProperty(document, "execCommand", { value: execCommand, configurable: true });

    await expect(copyText("x")).resolves.toBe(true);
    expect(execCommand).toHaveBeenCalled();
  });

  it("两条路径都失败时返回 false，交给调用方提示手动复制", async () => {
    vi.stubGlobal("navigator", {});
    Object.defineProperty(document, "execCommand", {
      value: vi.fn(() => { throw new Error("unsupported"); }),
      configurable: true,
    });

    await expect(copyText("x")).resolves.toBe(false);
  });
});
