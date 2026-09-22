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
  provider_binding: "",
  allowed_ips: "",
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
    // 新 Key 面板的复制按钮（页面上还有 Base URL/curl/Python 三处复制）
    const keyCopy = screen.getByTestId("new-key-plaintext").parentElement!.querySelector("button")!;
    await userEvent.click(keyCopy);

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

  it("展示渠道绑定与 IP 白名单，空值给出「自动 / 不限制」", async () => {
    const scoped = { ...KEY, id: "key_2", provider_binding: "trae", allowed_ips: "203.0.113.9/32" };
    mockFetch({ "/api/api-keys": { api_keys: [KEY, scoped] } });
    renderPage(<ApiKeysPage />);
    await settle();

    expect(screen.getByTestId("key-binding-key_1")).toHaveTextContent("自动");
    expect(screen.getByTestId("key-ips-key_1")).toHaveTextContent("不限制");
    expect(screen.getByTestId("key-binding-key_2")).toHaveTextContent("TRAE");
    expect(screen.getByTestId("key-ips-key_2")).toHaveTextContent("203.0.113.9/32");
  });

  it("创建时把渠道绑定与 IP 白名单一起提交", async () => {
    const calls: Array<{ url: string; body: unknown }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url === "/api/api-keys" && init?.method === "POST") {
          calls.push({ url, body: JSON.parse(String(init.body)) });
          return jsonResponse({ ...KEY, provider_binding: "codebuddy", allowed_ips: "10.0.0.0/8", api_key: "sk-x" });
        }
        return jsonResponse({ api_keys: [] });
      }),
    );
    renderPage(<ApiKeysPage />);
    await settle();

    await userEvent.type(screen.getByTestId("key-name"), "ci");
    await userEvent.selectOptions(screen.getByTestId("key-binding"), "codebuddy");
    await userEvent.type(screen.getByTestId("key-allowed-ips"), "10.0.0.0/8");
    await userEvent.click(screen.getByRole("button", { name: "创建" }));

    await screen.findByTestId("new-key-plaintext");
    expect(calls).toEqual([
      { url: "/api/api-keys", body: { name: "ci", provider_binding: "codebuddy", allowed_ips: "10.0.0.0/8" } },
    ]);
  });

  it("创建失败时提示检查绑定与白名单格式", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === "POST") {
          return jsonResponse({ error: { message: "bad" } }, 400);
        }
        return jsonResponse({ api_keys: [] });
      }),
    );
    renderPage(<ApiKeysPage />);
    await settle();

    await userEvent.click(screen.getByRole("button", { name: "创建" }));
    expect(await screen.findByText(/请检查渠道绑定与 IP 白名单格式/)).toBeInTheDocument();
  });
});

describe("OpenAI 客户端接入面板", () => {
  it("显示 Base URL 与端点，未创建 Key 时示例用占位符", async () => {
    mockFetch({ "/api/api-keys": { api_keys: [] } });
    renderPage(<ApiKeysPage />);
    await settle();

    const baseUrl = screen.getByTestId("openai-base-url").textContent;
    expect(baseUrl).toBe(`${window.location.origin}/v1`);
    expect(screen.getByTestId("openai-entry")).toHaveTextContent("/chat/completions");
    expect(screen.getByTestId("openai-entry")).toHaveTextContent("/models");
    expect(screen.getByTestId("openai-entry")).toHaveTextContent("/user/balance");
    expect(screen.getByTestId("example-curl")).toHaveTextContent("sk-…");
    expect(screen.getByTestId("example-python")).toHaveTextContent(
      `base_url="${window.location.origin}/v1"`,
    );
    expect(screen.getByTestId("example-balance-curl")).toHaveTextContent(
      `curl ${window.location.origin}/v1/user/balance`,
    );
    expect(screen.getByTestId("example-balance-curl")).toHaveTextContent("sk-…");
    // Responses 端点在此列出，示例默认折叠
    expect(screen.getByTestId("example-details-responses")).toHaveTextContent("/responses");
    expect(screen.getByTestId("openai-entry")).toHaveTextContent("Codex CLI");
  });
  it("创建 Key 后示例自动带入真实 Key", async () => {
    const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/api-keys" && init?.method === "POST") {
        return jsonResponse({ ...KEY, api_key: "sk-real-key" });
      }
      if (url === "/api/api-keys") return jsonResponse({ api_keys: [] });
      throw new Error(`未 mock: ${url}`);
    });
    vi.stubGlobal("fetch", fetchSpy);
    renderPage(<ApiKeysPage />);
    await settle();

    await userEvent.type(screen.getByTestId("key-name"), "test");
    await userEvent.click(screen.getByRole("button", { name: "创建" }));
    await waitFor(() => {
      expect(screen.getByTestId("example-curl")).toHaveTextContent("sk-real-key");
    });
    expect(screen.getByTestId("example-python")).toHaveTextContent('api_key="sk-real-key"');
    expect(screen.getByTestId("example-balance-curl")).toHaveTextContent("sk-real-key");
    expect(screen.getByTestId("example-responses")).toHaveTextContent(
      "export CODING2API_KEY=sk-real-key",
    );
  });
});

it("调用示例默认折叠，点击端点行展开", async () => {
  mockFetch({ "/api/api-keys": { api_keys: [] } });
  renderPage(<ApiKeysPage />);
  await settle();

  const trigger = screen.getByTestId("example-details-trigger");
  expect(trigger).toHaveAttribute("data-state", "closed");
  await userEvent.click(trigger);
  expect(trigger).toHaveAttribute("data-state", "open");
  expect(screen.getByTestId("example-curl")).toBeVisible();
});

it("余额端点示例同样默认折叠，展开后可复制 curl", async () => {
  const writeText = vi.fn(async () => undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  mockFetch({ "/api/api-keys": { api_keys: [] } });
  renderPage(<ApiKeysPage />);
  await settle();

  const trigger = screen.getByTestId("example-details-balance-trigger");
  expect(trigger).toHaveAttribute("data-state", "closed");
  await userEvent.click(trigger);
  expect(trigger).toHaveAttribute("data-state", "open");

  const copy = screen.getByTestId("copy-balance-curl");
  await userEvent.click(copy);
  expect(writeText).toHaveBeenCalledWith(
    `curl ${window.location.origin}/v1/user/balance \\
  -H "Authorization: Bearer sk-…"`,
  );
});

it("Responses 端点示例默认折叠，展开后可复制 Codex CLI 配置", async () => {
  const writeText = vi.fn(async () => undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  mockFetch({ "/api/api-keys": { api_keys: [] } });
  renderPage(<ApiKeysPage />);
  await settle();

  const trigger = screen.getByTestId("example-details-responses-trigger");
  expect(trigger).toHaveAttribute("data-state", "closed");
  // 折叠时子节点仍在 DOM 里（forceMount），靠 data-state 控制可见性
  expect(screen.getByTestId("example-responses").closest("[data-state]")).toHaveAttribute(
    "data-state",
    "closed",
  );

  await userEvent.click(trigger);
  expect(trigger).toHaveAttribute("data-state", "open");
  expect(screen.getByTestId("example-responses")).toBeVisible();
  expect(screen.getByTestId("example-responses")).toHaveTextContent("wire_api='responses'");
  expect(screen.getByTestId("example-responses")).toHaveTextContent(
    `base_url='${window.location.origin}/v1'`,
  );

  await userEvent.click(screen.getByTestId("copy-responses"));
  expect(writeText).toHaveBeenCalledWith(
    expect.stringContaining("export CODING2API_KEY=sk-…"),
  );
});
