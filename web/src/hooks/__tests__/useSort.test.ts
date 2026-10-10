import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useSort } from "../useSort";

describe("useSort", () => {
  it("默认列与方向", () => {
    const { result } = renderHook(() => useSort("ts", "desc"));
    expect(result.current.sort).toBe("ts");
    expect(result.current.order).toBe("desc");
  });

  it("点当前列在 asc/desc 间切换", () => {
    const { result } = renderHook(() => useSort("ts", "desc"));
    act(() => result.current.toggle("ts"));
    expect(result.current).toMatchObject({ sort: "ts", order: "asc" });
    act(() => result.current.toggle("ts"));
    expect(result.current).toMatchObject({ sort: "ts", order: "desc" });
  });

  it("点新列用该列的首次点击方向（缺省用默认方向）", () => {
    const { result } = renderHook(() => useSort("ts", "asc", { requests: "desc" }));
    act(() => result.current.toggle("requests"));
    expect(result.current).toMatchObject({ sort: "requests", order: "desc" });
    // 未配置首次方向的列 → 用默认 asc
    act(() => result.current.toggle("model"));
    expect(result.current).toMatchObject({ sort: "model", order: "asc" });
  });
});
