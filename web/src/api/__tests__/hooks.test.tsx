import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useCredentials } from "../hooks";
import { jsonResponse } from "@/test-utils";

/** 渲染 hook 并返回它写进缓存的 query key（这是唯一能验证 key 的地方）。 */
function renderCredentials(...args: Parameters<typeof useCredentials>) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const view = renderHook(() => useCredentials(...args), { wrapper });
  return { ...view, client };
}

describe("useCredentials", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("username 进 query key：与凭证页共用同一套缓存，不再落到 anonymous", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ credentials: [] })));
    const { result, client } = renderCredentials("alice", undefined, "created_at", "asc");
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const keys = client.getQueryCache().getAll().map((query) => query.queryKey);
    expect(keys).toContainEqual(["admin", "alice", "credentials", "created_at", "asc"]);
    expect(keys.some((key) => key[1] === "anonymous")).toBe(false);
  });

  it("不传 username 时保持向后兼容（落到 anonymous，请求不带身份参数）", async () => {
    const spy = vi.fn(async () => jsonResponse({ credentials: [] }));
    vi.stubGlobal("fetch", spy);
    const { result, client } = renderCredentials();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(client.getQueryCache().getAll()[0].queryKey)
      .toEqual(["admin", "anonymous", "credentials", undefined, undefined]);
    expect(spy).toHaveBeenCalledWith("/api/credentials", expect.anything());
  });
});
