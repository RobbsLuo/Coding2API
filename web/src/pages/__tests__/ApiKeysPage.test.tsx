import { screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiKeysPage } from "../ApiKeysPage";
import { jsonResponse, mockFetch, renderPage, settle, userEvent } from "./helpers";

const KEY = {
  id: "key_1",
  username: "root",
  name: "laptop",
  preview: "sk-…Ab3d",
  created_at: 1_700_000_000,
  last_used_at: null,
};

describe("ApiKeysPage", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("空列表提示", async () => {
    mockFetch({ "/api/api-keys": { api_keys: [] } });
    renderPage(<ApiKeysPage />);
    await settle();
    expect(screen.getByTestId("no-keys")).toBeInTheDocument();
  });

  it("渲染 Key 列表，未使用的显示「从未使用」", async () => {
    mockFetch({ "/api/api-keys": { api_keys: [KEY] } });
    renderPage(<ApiKeysPage />);
    await settle();

    expect(screen.getByTestId("keys-table")).toHaveTextContent("laptop");
    expect(screen.getByTestId("keys-table")).toHaveTextContent("sk-…Ab3d");
    expect(screen.getByText("从未使用")).toBeInTheDocument();
  });

  it("创建后展示一次性明文并提示只显示一次", async () => {
    mockFetch({
      "/api/api-keys": (() => {
        let called = false;
        return () => {
          if (!called) {
            called = true;
            return jsonResponse({
              api_keys: [],
            });
          }
          return jsonResponse({ api_keys: [KEY] });
        };
      })(),
    });
    const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/api-keys" && init?.method === "POST") {
        return jsonResponse({ ...KEY, api_key: "sk-plaintext-value" });
      }
      if (url === "/api/api-keys") return jsonResponse({ api_keys: [] });
      throw new Error(`未 mock: ${url}`);
    });
    vi.stubGlobal("fetch", fetchSpy);

    renderPage(<ApiKeysPage />);
    await settle();
    await userEvent.type(screen.getByTestId("key-name"), "laptop");
    await userEvent.click(screen.getByRole("button", { name: "创建" }));

    expect(await screen.findByTestId("new-key-plaintext")).toHaveTextContent("sk-plaintext-value");
    expect(screen.getByText(/只会显示这一次/)).toBeInTheDocument();
  });

  it("复制后给出反馈", async () => {
    const writeText = vi.fn(async () => undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url === "/api/api-keys" && init?.method === "POST") {
          return jsonResponse({ ...KEY, api_key: "sk-copy-me" });
        }
        return jsonResponse({ api_keys: [] });
      }),
    );

    renderPage(<ApiKeysPage />);
    await settle();
    await userEvent.click(screen.getByRole("button", { name: "创建" }));
    await screen.findByTestId("new-key-plaintext");
    await userEvent.click(screen.getByRole("button", { name: "复制" }));

    expect(writeText).toHaveBeenCalledWith("sk-copy-me");
    expect(await screen.findByRole("button", { name: "已复制" })).toBeInTheDocument();
  });

  it("删除需要二次确认", async () => {
    const fetchSpy = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "DELETE") return jsonResponse({ ok: true });
      return jsonResponse({ api_keys: [KEY] });
    });
    vi.stubGlobal("fetch", fetchSpy);

    renderPage(<ApiKeysPage />);
    await settle();

    await userEvent.click(screen.getByRole("button", { name: "删除" }));
    expect(screen.getByRole("button", { name: "确认删除" })).toBeInTheDocument();
    expect(fetchSpy).not.toHaveBeenCalledWith(
      expect.stringContaining("/api/api-keys/key_1"),
      expect.objectContaining({ method: "DELETE" }),
    );

    await userEvent.click(screen.getByRole("button", { name: "确认删除" }));
    await waitFor(() =>
      expect(fetchSpy).toHaveBeenCalledWith(
        "/api/api-keys/key_1",
        expect.objectContaining({ method: "DELETE" }),
      ),
    );
  });

  it("取消删除不会发起请求", async () => {
    const fetchSpy = vi.fn(async () => jsonResponse({ api_keys: [KEY] }));
    vi.stubGlobal("fetch", fetchSpy);
    renderPage(<ApiKeysPage />);
    await settle();

    await userEvent.click(screen.getByRole("button", { name: "删除" }));
    await userEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.getByRole("button", { name: "删除" })).toBeInTheDocument();
  });
});
