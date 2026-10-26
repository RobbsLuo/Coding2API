/**
 * Key 级配额前端：解析/摘要、创建时提交、列表展示与「改配额」对话框。
 */
import { screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  EMPTY_QUOTA,
  parseQuota,
  quotaInput,
  quotaOf,
  quotaSummary,
  setApiKeyQuota,
  usageTodayLabel,
  usageTodayOf,
} from "../keyQuota";
import { ApiKeysPage } from "../../pages/ApiKeysPage";
import { jsonResponse, renderPage, settle, userEvent } from "@/test-utils";

const KEY = {
  id: "key_1",
  username: "root",
  name: "laptop",
  preview: "sk-…Ab3d",
  created_at: 1_700_000_000,
  last_used_at: null,
  provider_binding: "",
  allowed_ips: "",
  allowed_models: "",
  expires_at: null,
  quota: "",
};

describe("keyQuota 纯函数", () => {
  it("parseQuota：空值/脏文本/数组都按不限制", () => {
    expect(parseQuota("")).toEqual(EMPTY_QUOTA);
    expect(parseQuota(null)).toEqual(EMPTY_QUOTA);
    expect(parseQuota("{bad json")).toEqual(EMPTY_QUOTA);
    expect(parseQuota("[1,2]")).toEqual(EMPTY_QUOTA);
    expect(parseQuota("null")).toEqual(EMPTY_QUOTA);
  });

  it("parseQuota：正常解析，负数/非数字归 0，未知键忽略", () => {
    const quota = parseQuota(
      JSON.stringify({ daily_requests: 10, rpm: -3, tpm: "abc", extra: 9 }),
    );
    expect(quota.daily_requests).toBe(10);
    expect(quota.rpm).toBe(0);
    expect(quota.tpm).toBe(0);
    expect(Object.keys(quota).sort()).toEqual(Object.keys(EMPTY_QUOTA).sort());
    // 也接受已解析对象
    expect(parseQuota({ daily_tokens: 5 }).daily_tokens).toBe(5);
  });

  it("quotaOf 从列表行取 quota 文本", () => {
    expect(quotaOf({ quota: '{"rpm":7}' }).rpm).toBe(7);
    expect(quotaOf({})).toEqual(EMPTY_QUOTA);
    expect(quotaOf(null)).toEqual(EMPTY_QUOTA);
  });

  it("quotaInput：全 0 → null，否则原样返回", () => {
    expect(quotaInput({ ...EMPTY_QUOTA })).toBeNull();
    expect(quotaInput({ ...EMPTY_QUOTA, rpm: 5 })).toEqual({ ...EMPTY_QUOTA, rpm: 5 });
  });

  it("quotaSummary 汇总六项，全关显示「不限制」", () => {
    expect(quotaSummary({ ...EMPTY_QUOTA })).toBe("不限制");
    expect(
      quotaSummary({
        daily_requests: 100,
        daily_tokens: 1000,
        daily_cost_usd: 2,
        rpm: 10,
        tpm: 5000,
        max_concurrency: 3,
      }),
    ).toBe("100 次/日 · 1000 token/日 · $2/日 · 10 RPM · 5000 TPM · 3 并发");
  });

  it("usageTodayOf / usageTodayLabel 兼容缺字段与零值", () => {
    expect(usageTodayOf({})).toEqual({ requests: 0, tokens: 0, cost_usd: 0 });
    expect(usageTodayOf(null)).toEqual({ requests: 0, tokens: 0, cost_usd: 0 });
    expect(usageTodayOf({ usage_today: { requests: 2, tokens: 30, cost_usd: 0.5 } }))
      .toEqual({ requests: 2, tokens: 30, cost_usd: 0.5 });
    expect(usageTodayLabel({ requests: 0, tokens: 0, cost_usd: 0 })).toBe("今日未用");
    expect(usageTodayLabel({ requests: 2, tokens: 30, cost_usd: 0 })).toBe("2 次 · 30 token");
    expect(usageTodayLabel({ requests: 2, tokens: 30, cost_usd: 0.5 }))
      .toBe("2 次 · 30 token · $0.5");
  });

  it("usageTodayLabel 格式化大数：请求数千分位、token 紧凑、成本按量级取位数", () => {
    expect(usageTodayLabel({ requests: 2974, tokens: 365_309_724, cost_usd: 7.4879 }))
      .toBe("2,974 次 · 3.7亿 token · $7.49");
    // 小额成本不能被两位小数抹成 $0
    expect(usageTodayLabel({ requests: 1, tokens: 100, cost_usd: 0.0042 }))
      .toBe("1 次 · 100 token · $0.0042");
  });
});

describe("setApiKeyQuota", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("PUT 到该 Key 的 quota 端点，带 CSRF 头", async () => {
    const fetchSpy = vi.fn(async () => jsonResponse({ ok: true }));
    vi.stubGlobal("fetch", fetchSpy);
    await setApiKeyQuota("key_1", { ...EMPTY_QUOTA, rpm: 5 });
    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/api-keys/key_1/quota",
      expect.objectContaining({
        method: "PUT",
        credentials: "same-origin",
        body: JSON.stringify({ quota: { ...EMPTY_QUOTA, rpm: 5 } }),
        headers: expect.objectContaining({ "X-Requested-With": "XMLHttpRequest" }),
      }),
    );
  });

  it("全 0 提交空对象（= 清除配额）", async () => {
    const fetchSpy = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      jsonResponse({ ok: true }));
    vi.stubGlobal("fetch", fetchSpy);
    await setApiKeyQuota("key_1", { ...EMPTY_QUOTA });
    const init = fetchSpy.mock.calls[0][1] as RequestInit;
    expect(JSON.parse(String(init.body))).toEqual({ quota: {} });
  });

  it("失败时抛出服务端错误文案", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ error: { message: "quota.rpm 不能为负数" } }, 400)),
    );
    await expect(setApiKeyQuota("key_1", { ...EMPTY_QUOTA, rpm: -1 }))
      .rejects.toThrow("quota.rpm 不能为负数");
  });

  it("非 JSON 错误体时回落 statusText", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("boom", { status: 500, statusText: "Server Error" })),
    );
    await expect(setApiKeyQuota("key_1", { ...EMPTY_QUOTA })).rejects.toThrow("Server Error");
  });
});

describe("ApiKeysPage 配额 UI", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("列表展示配额摘要与当日已用", async () => {
    const limited = {
      ...KEY,
      id: "key_2",
      quota: JSON.stringify({ daily_requests: 100, rpm: 5 }),
      usage_today: { requests: 3, tokens: 1200, cost_usd: 0.25 },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ api_keys: [KEY, limited] })),
    );
    renderPage(<ApiKeysPage />);
    await settle();

    expect(screen.getByTestId("key-quota-key_1")).toHaveTextContent("不限制");
    expect(screen.getByTestId("key-usage-key_1")).toHaveTextContent("今日未用");
    expect(screen.getByTestId("key-quota-key_2")).toHaveTextContent("100 次/日");
    expect(screen.getByTestId("key-quota-key_2")).toHaveTextContent("5 RPM");
    expect(screen.getByTestId("key-usage-key_2")).toHaveTextContent("3 次");
  });

  it("创建时不填配额就不上送 quota 字段", async () => {
    const calls: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input) === "/api/api-keys" && init?.method === "POST") {
          calls.push(JSON.parse(String(init.body)));
          return jsonResponse({ ...KEY, api_key: "sk-x" });
        }
        return jsonResponse({ api_keys: [] });
      }),
    );
    renderPage(<ApiKeysPage />);
    await settle();
    await userEvent.click(screen.getByTestId("open-create-key-dialog"));
    await userEvent.type(screen.getByTestId("key-name"), "no-quota");
    await userEvent.click(screen.getByRole("button", { name: "创建" }));
    await screen.findByTestId("new-key-plaintext");
    expect(calls).toEqual([
      { name: "no-quota", provider_binding: "", allowed_ips: "", allowed_models: "", expires_at: null },
    ]);
  });

  it("创建时填了配额就随 body 提交", async () => {
    const calls: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input) === "/api/api-keys" && init?.method === "POST") {
          calls.push(JSON.parse(String(init.body)));
          return jsonResponse({ ...KEY, api_key: "sk-x" });
        }
        return jsonResponse({ api_keys: [] });
      }),
    );
    renderPage(<ApiKeysPage />);
    await settle();
    await userEvent.click(screen.getByTestId("open-create-key-dialog"));
    // 配额区块默认收起，展开后填一项
    await userEvent.click(screen.getByTestId("key-quota-trigger"));
    await userEvent.type(screen.getByTestId("key-quota-input-rpm"), "5");
    await userEvent.click(screen.getByRole("button", { name: "创建" }));
    await screen.findByTestId("new-key-plaintext");
    expect(calls).toEqual([
      {
        name: "",
        provider_binding: "",
        allowed_ips: "",
        allowed_models: "",
        expires_at: null,
        quota: { ...EMPTY_QUOTA, rpm: 5 },
      },
    ]);
  });

  it("「配额」按钮打开对话框并 PUT 保存", async () => {
    const puts: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === "PUT") {
          puts.push(JSON.parse(String(init.body)));
          return jsonResponse({ ok: true });
        }
        return jsonResponse({ api_keys: [KEY] });
      }),
    );
    renderPage(<ApiKeysPage />);
    await settle();

    await userEvent.click(screen.getByTestId("edit-quota-key_1"));
    expect(screen.getByTestId("key-quota-dialog")).toBeInTheDocument();
    await userEvent.type(screen.getByTestId("key-quota-edit-daily_requests"), "50");
    await userEvent.click(screen.getByTestId("key-quota-save"));

    await waitFor(() => expect(puts).toEqual([
      { quota: { ...EMPTY_QUOTA, daily_requests: 50 } },
    ]));
    await waitFor(() => expect(screen.queryByTestId("key-quota-dialog")).not.toBeInTheDocument());
  });

  it("保存失败时在对话框里显示服务端文案", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === "PUT") {
          return jsonResponse({ error: { message: "api key not found" } }, 400);
        }
        return jsonResponse({ api_keys: [KEY] });
      }),
    );
    renderPage(<ApiKeysPage />);
    await settle();

    await userEvent.click(screen.getByTestId("edit-quota-key_1"));
    await userEvent.click(screen.getByTestId("key-quota-save"));
    expect(await screen.findByText("api key not found")).toBeInTheDocument();
    expect(screen.getByTestId("key-quota-dialog")).toBeInTheDocument();
  });

  it("对话框可关闭，且全 0 保存 = 清除配额", async () => {
    const puts: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === "PUT") {
          puts.push(JSON.parse(String(init.body)));
          return jsonResponse({ ok: true });
        }
        return jsonResponse({ api_keys: [{ ...KEY, quota: '{"rpm":5}' }] });
      }),
    );
    renderPage(<ApiKeysPage />);
    await settle();

    await userEvent.click(screen.getByTestId("edit-quota-key_1"));
    // 回显现有配额
    expect(screen.getByTestId("key-quota-edit-rpm")).toHaveValue(5);
    await userEvent.clear(screen.getByTestId("key-quota-edit-rpm"));
    await userEvent.click(screen.getByTestId("key-quota-save"));
    await waitFor(() => expect(puts).toEqual([{ quota: {} }]));

    await userEvent.click(screen.getByTestId("edit-quota-key_1"));
    await userEvent.click(screen.getByTestId("key-quota-close"));
    expect(screen.queryByTestId("key-quota-dialog")).not.toBeInTheDocument();
  });

  it("Esc 关闭对话框（焦点管理：MASTER §6「对话框焦点管理与 Esc 关闭」）", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === "PUT") return jsonResponse({ ok: true });
        return jsonResponse({ api_keys: [KEY] });
      }),
    );
    renderPage(<ApiKeysPage />);
    await settle();

    await userEvent.click(screen.getByTestId("edit-quota-key_1"));
    expect(screen.getByTestId("key-quota-dialog")).toBeInTheDocument();

    // Esc 关闭：此前这个对话框没接 useDialogFocus，Esc 收不掉它
    await userEvent.keyboard("{Escape}");
    await waitFor(() =>
      expect(screen.queryByTestId("key-quota-dialog")).not.toBeInTheDocument());
  });
});
