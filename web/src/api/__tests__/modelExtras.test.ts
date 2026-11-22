import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../client";
import {
  aliasTitle,
  blocklistPatterns,
  fetchModelCatalog,
  isCustomAlias,
  modelCost,
  reasoningEfforts,
  withBlocklistPattern,
} from "../modelExtras";
import type { ModelInfo } from "../types";

const base = (extra: Record<string, unknown> = {}): ModelInfo => ({
  id: "glm-5.2",
  object: "model",
  owned_by: "Coding2API",
  providers: ["trae"],
  ...extra,
});

describe("别名 / 价格 / 档位读取", () => {
  it("老后端条目（无新字段）一律按「没有」处理", () => {
    const item = base();
    expect(isCustomAlias(item)).toBe(false);
    expect(modelCost(item)).toBeUndefined();
    expect(reasoningEfforts(item)).toEqual([]);
    expect(aliasTitle(item)).toBe("用户自定义别名（MODEL_ALIASES）");
  });

  it("自定义别名条目带标记与目标", () => {
    const item = base({ custom_alias: true, alias_target: "glm-5.2" });
    expect(isCustomAlias(item)).toBe(true);
    expect(aliasTitle(item)).toBe("用户自定义别名，指向 glm-5.2（MODEL_ALIASES）");
  });

  it("价格三项原样读取；缺价不带字段（不是 0）", () => {
    expect(modelCost(base({ cost: { input: 1, output: 2, cache_read: 0.1 } })))
      .toEqual({ input: 1, output: 2, cache_read: 0.1 });
    expect(modelCost(base())).toBeUndefined();
  });

  it("档位未配置返回空数组", () => {
    expect(reasoningEfforts(base({ reasoning_efforts: ["high", "low"] })))
      .toEqual(["high", "low"]);
    expect(reasoningEfforts(base())).toEqual([]);
  });
});

describe("黑名单文本处理", () => {
  it("按逗号切分并丢弃空白项", () => {
    expect(blocklistPatterns(" a ,, b ,")).toEqual(["a", "b"]);
    expect(blocklistPatterns("")).toEqual([]);
  });

  it("追加一条模式，保留既有项（不覆盖安全配置）", () => {
    expect(withBlocklistPattern("custom_model_*,summary", "glm-5.2"))
      .toBe("custom_model_*,summary,glm-5.2");
  });

  it("空黑名单追加即该条；已存在（大小写不敏感）不重复追加", () => {
    expect(withBlocklistPattern("", "glm-5.2")).toBe("glm-5.2");
    expect(withBlocklistPattern("GLM-5.2", "glm-5.2")).toBe("GLM-5.2");
    // 已存在时也顺手清掉多余空白（与后端解析同口径）
    expect(withBlocklistPattern(" a , GLM-5.2 ", "glm-5.2")).toBe("a,GLM-5.2");
  });

  it("空白模式不写入（按钮不该往黑名单里塞空项）", () => {
    expect(withBlocklistPattern("a", "   ")).toBe("a");
  });
});

describe("非 JSON 响应", () => {
  it("反代回 HTML 错误页时抛可读的 ApiError，而不是 SyntaxError", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      "<html><body>502 Bad Gateway</body></html>",
      { status: 200, headers: { "Content-Type": "text/html" } },
    )));
    const caught = await fetchModelCatalog().catch((error: unknown) => error);
    expect(caught).toBeInstanceOf(ApiError);
    expect((caught as ApiError).code).toBe("invalid_response");
    expect((caught as ApiError).message).toContain("非 JSON");
  });

  it("错误状态且体不是 JSON 时仍保留真实 HTTP 状态", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      "<html>502</html>", { status: 502, headers: { "Content-Type": "text/html" } },
    )));
    const caught = await fetchModelCatalog().catch((error: unknown) => error);
    expect(caught).toBeInstanceOf(ApiError);
    expect((caught as ApiError).status).toBe(502);
  });

  it("合法 JSON 照常解析（空体按 null 处理）", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      JSON.stringify({ models: [], total: 0 }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    )));
    await expect(fetchModelCatalog()).resolves.toMatchObject({ total: 0 });
  });
});
