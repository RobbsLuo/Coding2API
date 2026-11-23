import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiKeysPage, parseExpiresAt } from "../ApiKeysPage";
import { expiresAtInput } from "../apikeys/meta";
import { jsonResponse, mockFetch, renderPage, settle, userEvent } from "@/test-utils";

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
    await openCreateDialog();
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
    await openCreateDialog();
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

  it("删除失败时透出后端文案（不只一句「删除失败」）", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === "DELETE") {
          return jsonResponse(
            { error: { code: "not_found", message: "Key 已不存在，可能已被其他会话删除" } },
            404,
          );
        }
        return jsonResponse({ api_keys: [KEY] });
      }),
    );
    renderPage(<ApiKeysPage />);
    await settle();

    await userEvent.click(screen.getByRole("button", { name: "删除" }));
    await userEvent.click(screen.getByRole("button", { name: "确认删除" }));

    const alert = await screen.findByTestId("api-keys-error");
    expect(alert).toHaveTextContent("可能已被其他会话删除");
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

  it("创建时把渠道绑定、IP 白名单、模型白名单与到期时间一起提交", async () => {
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
    await openCreateDialog();

    await userEvent.type(screen.getByTestId("key-name"), "ci");
    await userEvent.selectOptions(screen.getByTestId("key-binding"), "codebuddy");
    await userEvent.type(screen.getByTestId("key-allowed-ips"), "10.0.0.0/8");
    await userEvent.type(screen.getByTestId("key-allowed-models"), "glm-*");
    // datetime-local 的 value 是本地时区；2026-01-02T03:04 在任意时区都解析为固定 epoch 秒
    const expiresInput = screen.getByTestId("key-expires-at");
    await userEvent.type(expiresInput, "2026-01-02T03:04");
    await userEvent.click(screen.getByRole("button", { name: "创建" }));

    await screen.findByTestId("new-key-plaintext");
    expect(calls).toEqual([
      {
        url: "/api/api-keys",
        body: {
          name: "ci",
          provider_binding: "codebuddy",
          allowed_ips: "10.0.0.0/8",
          allowed_models: "glm-*",
          expires_at: Math.floor(new Date("2026-01-02T03:04").getTime() / 1000),
        },
      },
    ]);
  });

  it("列表展示模型白名单与到期时间，空值给出「不限制 / 永不过期」", async () => {
    const scoped = {
      ...KEY,
      id: "key_2",
      allowed_models: "glm-*,kimi-k3",
      expires_at: 1_800_000_000,
    };
    mockFetch({ "/api/api-keys": { api_keys: [KEY, scoped] } });
    renderPage(<ApiKeysPage />);
    await settle();

    expect(screen.getByTestId("key-models-key_1")).toHaveTextContent("不限制");
    expect(screen.getByTestId("key-expires-key_1")).toHaveTextContent("永不过期");
    expect(screen.getByTestId("key-models-key_2")).toHaveTextContent("glm-*,kimi-k3");
    expect(screen.getByTestId("key-expires-key_2")).not.toHaveTextContent("永不过期");
  });

  it("parseExpiresAt：空串 → null，非法值抛错，合法值 → epoch 秒", () => {
    expect(parseExpiresAt("   ")).toBeNull();
    expect(() => parseExpiresAt("not-a-date")).toThrow();
    expect(parseExpiresAt("2026-01-02T03:04")).toBe(
      Math.floor(new Date("2026-01-02T03:04").getTime() / 1000),
    );
  });

  it("创建失败时透出后端的受控原因（不只给通用兜底）", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === "POST") {
          return jsonResponse(
            { error: { message: "渠道绑定与已有 Key 冲突" } }, 400);
        }
        return jsonResponse({ api_keys: [] });
      }),
    );
    renderPage(<ApiKeysPage />);
    await settle();
    await openCreateDialog();

    await userEvent.click(screen.getByRole("button", { name: "创建" }));
    // 后端写好的受控文案要透出来：裸 catch{} 会把它丢掉，用户只能对着
    // 「创建失败」猜到底是哪一项不对。
    expect(await screen.findByText(/渠道绑定与已有 Key 冲突/)).toBeInTheDocument();
  });

  it("创建失败且后端无可用文案时回落通用兜底", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === "POST") {
          // 非受控异常（网络层）：没有可展示的 message
          throw new TypeError("Failed to fetch");
        }
        return jsonResponse({ api_keys: [] });
      }),
    );
    renderPage(<ApiKeysPage />);
    await settle();
    await openCreateDialog();

    await userEvent.click(screen.getByRole("button", { name: "创建" }));
    expect(await screen.findByText(/检查渠道绑定、模型白名单、IP 白名单与到期时间格式/)).toBeInTheDocument();
  });

  it("无 Clipboard API 时复制一次性 Key 失败会提示手动复制", async () => {
    vi.stubGlobal("navigator", {});
    Object.defineProperty(document, "execCommand", {
      value: vi.fn(() => false),
      configurable: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === "POST") {
          return jsonResponse({ ...KEY, api_key: "sk-plaintext-value" });
        }
        return jsonResponse({ api_keys: [] });
      }),
    );
    renderPage(<ApiKeysPage />);
    await settle();
    await openCreateDialog();
    await userEvent.click(screen.getByRole("button", { name: "创建" }));
    await screen.findByTestId("new-key-plaintext");

    const keyCopy = screen.getByTestId("new-key-plaintext").parentElement!.querySelector("button")!;
    await userEvent.click(keyCopy);
    // 一次性明文复制失败必须说清楚，否则用户关掉就再也拿不回来
    expect(await screen.findByTestId("new-key-copy-failed")).toHaveTextContent("请手动");
    expect(screen.queryByRole("button", { name: "已复制" })).not.toBeInTheDocument();
  });
});

/** 打开「创建 API Key」对话框（由「我的 API Key」右上角按钮触发）。 */
async function openCreateDialog() {
  await userEvent.click(screen.getByTestId("open-create-key-dialog"));
  expect(await screen.findByTestId("create-key-dialog")).toBeInTheDocument();
}


it("面板默认收起，点「说明」展开、点「收起」收拢", async () => {
  mockFetch({ "/api/api-keys": { api_keys: [] } });
  renderPage(<ApiKeysPage />);
  await settle();

  const toggle = screen.getByTestId("openai-entry-toggle");
  expect(toggle).toHaveTextContent("说明");
  expect(screen.queryByTestId("openai-entry")).not.toBeInTheDocument();

  await userEvent.click(toggle);
  expect(toggle).toHaveTextContent("收起");
  expect(screen.getByTestId("openai-entry")).toBeVisible();
  expect(screen.getByTestId("openai-base-url")).toBeVisible();

  await userEvent.click(toggle);
  expect(toggle).toHaveTextContent("说明");
  expect(screen.queryByTestId("openai-entry")).not.toBeInTheDocument();
});

it("创建 Key 收在对话框：右上角按钮打开，创建成功后展示一次性明文，可关闭", async () => {
  const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/api-keys" && init?.method === "POST") {
      return jsonResponse({ ...KEY, api_key: "sk-dialog-key" });
    }
    return jsonResponse({ api_keys: [] });
  });
  vi.stubGlobal("fetch", fetchSpy);
  renderPage(<ApiKeysPage />);
  await settle();

  // 表单默认不在页面上，点右上角按钮才出现
  expect(screen.queryByTestId("key-name")).not.toBeInTheDocument();
  await openCreateDialog();

  await userEvent.type(screen.getByTestId("key-name"), "dialog");
  await userEvent.click(screen.getByRole("button", { name: "创建" }));
  expect(await screen.findByTestId("new-key-plaintext")).toHaveTextContent("sk-dialog-key");

  // 「我已保存」关闭对话框
  await userEvent.click(screen.getByRole("button", { name: "我已保存" }));
  expect(screen.queryByTestId("create-key-dialog")).not.toBeInTheDocument();
  expect(screen.queryByTestId("new-key-plaintext")).not.toBeInTheDocument();
  // L3：关闭后明文从页面内存抹掉，接入面板不再残留该 Key
  expect(screen.queryByText(/sk-dialog-key/)).not.toBeInTheDocument();
});

it("创建 Key 对话框支持 Esc 关闭", async () => {
  mockFetch({ "/api/api-keys": { api_keys: [] } });
  renderPage(<ApiKeysPage />);
  await settle();
  await openCreateDialog();
  await userEvent.keyboard("{Escape}");
  expect(screen.queryByTestId("create-key-dialog")).not.toBeInTheDocument();
});

it("创建中连点只发一次 POST（每次 POST 都会生成不可回收的明文 Key）", async () => {
  let resolvePost: ((value: Response) => void) | null = null;
  let posts = 0;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/api-keys" && init?.method === "POST") {
      posts += 1;
      // 挂住第一次请求：给第二次点击留出窗口
      return new Promise<Response>((resolve) => {
        resolvePost = resolve;
      });
    }
    return jsonResponse({ api_keys: [] });
  }));
  renderPage(<ApiKeysPage />);
  await settle();
  await openCreateDialog();

  const submit = screen.getByTestId("create-key-submit");
  await userEvent.click(submit);
  // 请求未返回：按钮已禁用并显示提交中文案
  await waitFor(() => expect(submit).toBeDisabled());
  expect(submit).toHaveTextContent("创建中");
  // 强行再点一次（disabled 之外还有 ref 互斥兜底）
  fireEvent.click(submit);
  fireEvent.submit(submit.closest("form")!);
  expect(posts).toBe(1);

  resolvePost!(jsonResponse({ ...KEY, api_key: "sk-once" }));
  expect(await screen.findByTestId("new-key-plaintext")).toHaveTextContent("sk-once");
  expect(posts).toBe(1);
});

describe("ApiKeysPage 首启向导深链", () => {
  it("?new=1 进来直接打开创建对话框", async () => {
    mockFetch({ "/api/api-keys": { api_keys: [] } });
    renderPage(<ApiKeysPage />, { username: "root", is_admin: true }, "/?new=1");
    expect(await screen.findByTestId("create-key-dialog")).toBeInTheDocument();
  });
});

describe("ApiKeysPage 用量窗口", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("默认今日；切到近 7 天带 window=7d 重新取数并改表头文案", async () => {
    const urls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        urls.push(url);
        // 服务端按窗口返回不同聚合：近 7 天的请求数必须来自新的一次取数
        if (url.includes("window=7d")) {
          return jsonResponse({
            api_keys: [{ ...KEY, usage: { requests: 5, tokens: 60, cost_usd: 0 } }],
            total: 1,
            window: "7d",
          });
        }
        return jsonResponse({
          api_keys: [{ ...KEY, usage: { requests: 1, tokens: 10, cost_usd: 0 } }],
          total: 1,
          window: "today",
        });
      }),
    );
    renderPage(<ApiKeysPage />);
    await settle();

    expect(screen.getByText("用量 · 今日")).toBeInTheDocument();
    expect(screen.getByTestId("key-usage-key_1")).toHaveTextContent("1 次");

    await userEvent.click(screen.getByTestId("usage-window-7d"));

    expect(await screen.findByText("用量 · 近 7 天")).toBeInTheDocument();
    expect(await screen.findByTestId("key-usage-key_1")).toHaveTextContent("5 次");
    expect(urls.some((url) => url.includes("window=7d"))).toBe(true);
    // 默认档位也要把窗口显式带给服务端（口径不靠服务端猜默认值）
    expect(urls.some((url) => url.includes("window=today"))).toBe(true);
  });

  it("没有用量时按档位显示「未用」", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        return jsonResponse({
          api_keys: [KEY],
          total: 1,
          window: url.includes("window=30d") ? "30d" : "today",
        });
      }),
    );
    renderPage(<ApiKeysPage />);
    await settle();
    expect(screen.getByTestId("key-usage-key_1")).toHaveTextContent("今日未用");

    await userEvent.click(screen.getByTestId("usage-window-30d"));
    expect(await screen.findByText("用量 · 近 30 天")).toBeInTheDocument();
  });
});

describe("ApiKeysPage 编辑策略", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  const EDITABLE = {
    ...KEY,
    allowed_ips: "10.0.0.1/32",
    allowed_models: "glm-*",
    expires_at: 4102444800,
  };

  it("回显五个字段并整体提交到 PUT /api/api-keys/{id}", async () => {
    const puts: { url: string; body: unknown }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (init?.method === "PUT") {
          puts.push({ url, body: JSON.parse(String(init.body)) });
          return jsonResponse({ ok: true, changed: ["name"] });
        }
        return jsonResponse({ api_keys: [EDITABLE], total: 1, window: "today" });
      }),
    );
    renderPage(<ApiKeysPage />);
    await settle();

    await userEvent.click(screen.getByTestId("edit-key-key_1"));
    expect(screen.getByTestId("edit-key-name")).toHaveValue("laptop");
    expect(screen.getByTestId("edit-key-allowed-models")).toHaveValue("glm-*");
    // 到期时间按**本地时区**回显：直接 toISOString 会差一个时区，一保存就改错
    expect(screen.getByTestId("edit-key-expires-at"))
      .toHaveValue(expiresAtInput(4102444800));

    await userEvent.clear(screen.getByTestId("edit-key-name"));
    await userEvent.type(screen.getByTestId("edit-key-name"), "renamed");
    await userEvent.click(screen.getByTestId("edit-key-save"));

    await waitFor(() =>
      expect(puts).toEqual([
        {
          url: "/api/api-keys/key_1",
          body: {
            name: "renamed",
            provider_binding: "",
            allowed_ips: "10.0.0.1/32",
            allowed_models: "glm-*",
            expires_at: 4102444800,
          },
        },
      ]),
    );
    // 保存成功后关闭对话框
    await waitFor(() =>
      expect(screen.queryByTestId("edit-key-dialog")).not.toBeInTheDocument());
  });

  it("保存失败时在对话框里显示服务端受控文案", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === "PUT") {
          return jsonResponse(
            { error: { code: "invalid_request", message: "allowed_ips 含非法 IP/CIDR: 'nope'" } },
            400,
          );
        }
        return jsonResponse({ api_keys: [EDITABLE], total: 1, window: "today" });
      }),
    );
    renderPage(<ApiKeysPage />);
    await settle();

    await userEvent.click(screen.getByTestId("edit-key-key_1"));
    await userEvent.click(screen.getByTestId("edit-key-save"));

    expect(await screen.findByText(/非法 IP\/CIDR/)).toBeInTheDocument();
    // 失败不关对话框：用户要能改完再存
    expect(screen.getByTestId("edit-key-dialog")).toBeInTheDocument();
  });

  it("Esc 关闭编辑对话框", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      api_keys: [EDITABLE], total: 1, window: "today",
    })));
    renderPage(<ApiKeysPage />);
    await settle();

    await userEvent.click(screen.getByTestId("edit-key-key_1"));
    expect(screen.getByTestId("edit-key-dialog")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    await waitFor(() =>
      expect(screen.queryByTestId("edit-key-dialog")).not.toBeInTheDocument());
  });
});
