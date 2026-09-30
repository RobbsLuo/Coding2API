import { describe, expect, it } from "vitest";
import type { ModelInfo } from "../../api/types";
import {
  buildSections,
  displayName,
  filterOptions,
  matchesFilter,
  modelRate,
  modelValue,
  pickDefaultModel,
  providerRate,
  type PickableModel,
} from "../ModelPicker";

const model = (overrides: Partial<ModelInfo> & Pick<ModelInfo, "id" | "providers">): ModelInfo => ({
  object: "model",
  owned_by: "Coding2API",
  ...overrides,
});

const pickable = (overrides: Partial<ModelInfo> & Pick<ModelInfo, "id" | "providers">): PickableModel => ({
  ...model(overrides),
  value: modelValue(overrides),
});

describe("modelValue", () => {
  it("单渠道带 @provider，多渠道用裸 id", () => {
    expect(modelValue({ id: "kimi", providers: ["trae"] })).toBe("kimi@trae");
    expect(modelValue({ id: "glm", providers: ["codebuddy", "trae"] })).toBe("glm");
  });
});

describe("providerRate", () => {
  it("优先按渠道细分倍率", () => {
    const item = model({
      id: "glm", providers: ["codebuddy", "trae"], credit_rate: 0.29,
      by_provider: { codebuddy: { credit_rate: 0.29 }, trae: { credit_rate: 0.17 } },
    });
    expect(providerRate(item, "codebuddy")).toBe(0.29);
    expect(providerRate(item, "trae")).toBe(0.17);
  });

  it("单渠道回退合并倍率", () => {
    const item = model({ id: "kimi", providers: ["trae"], credit_rate: 0.08 });
    expect(providerRate(item, "trae")).toBe(0.08);
  });

  it("多渠道缺细分倍率时不拿合并值冒充", () => {
    const item = model({
      id: "glm", providers: ["codebuddy", "trae"], credit_rate: 0.29,
      by_provider: { codebuddy: { credit_rate: 0.29 } },
    });
    expect(providerRate(item, "trae")).toBeUndefined();
  });
});

describe("modelRate", () => {
  it("多渠道取各渠道最小倍率", () => {
    const item = model({
      id: "glm", providers: ["codebuddy", "trae"],
      by_provider: { codebuddy: { credit_rate: 0.29 }, trae: { credit_rate: 0.17 } },
    });
    expect(modelRate(item)).toBe(0.17);
  });

  it("无渠道倍率时回退合并倍率；完全没有则 undefined", () => {
    expect(modelRate(model({ id: "a", providers: ["trae"], credit_rate: 0.08 }))).toBe(0.08);
    expect(modelRate(model({ id: "b", providers: ["trae"] }))).toBeUndefined();
  });
});

describe("pickDefaultModel", () => {
  it("选倍率最小的模型；无倍率时回退第一个", () => {
    const models = [
      pickable({ id: "glm", providers: ["codebuddy", "trae"],
        by_provider: { codebuddy: { credit_rate: 0.29 }, trae: { credit_rate: 0.17 } } }),
      pickable({ id: "kimi", providers: ["trae"], credit_rate: 0.08 }),
    ];
    expect(pickDefaultModel(models)).toBe("kimi@trae");
    expect(pickDefaultModel([pickable({ id: "x", providers: ["trae"] })])).toBe("x@trae");
    expect(pickDefaultModel([])).toBe("");
  });
});

describe("buildSections", () => {
  it("多渠道置顶，单渠道按展示顺序分组", () => {
    const models = [
      pickable({ id: "aaa-zen", providers: ["zen"] }),
      pickable({ id: "kimi", providers: ["trae"] }),
      pickable({ id: "deepseek", providers: ["codebuddy"] }),
      pickable({ id: "glm", providers: ["codebuddy", "trae"] }),
    ];
    expect(buildSections(models).map((section) => [section.key, section.label])).toEqual([
      ["auto", "多渠道（自动调度）"],
      ["codebuddy", "仅 CodeBuddy"],
      ["trae", "仅 TRAE"],
      ["zen", "仅 OpenCode Zen"],
    ]);
  });

  it("没有多渠道模型时不产生自动分组", () => {
    const sections = buildSections([pickable({ id: "kimi", providers: ["trae"] })]);
    expect(sections.map((section) => section.key)).toEqual(["trae"]);
  });
});

describe("filterOptions", () => {
  it("全部 + 多渠道 + 出现过的渠道（按展示顺序）", () => {
    const models = [
      pickable({ id: "glm", providers: ["codebuddy", "trae"] }),
      pickable({ id: "aaa-zen", providers: ["zen"] }),
    ];
    expect(filterOptions(models)).toEqual([
      { value: "all", label: "全部" },
      { value: "auto", label: "多渠道" },
      { value: "codebuddy", label: "CodeBuddy" },
      { value: "trae", label: "TRAE" },
      { value: "zen", label: "OpenCode Zen" },
    ]);
  });

  it("没有多渠道模型时不提供「多渠道」筛选", () => {
    expect(filterOptions([pickable({ id: "kimi", providers: ["trae"] })]).map((o) => o.value))
      .toEqual(["all", "trae"]);
  });
});

describe("matchesFilter", () => {
  const dual = pickable({ id: "glm-5.2", providers: ["codebuddy", "trae"] });
  const single = pickable({ id: "kimi-k3", providers: ["trae"] });

  it("渠道筛选：多渠道只留多渠道，指定渠道按 providers 命中", () => {
    expect(matchesFilter(single, "", "auto")).toBe(false);
    expect(matchesFilter(dual, "", "auto")).toBe(true);
    expect(matchesFilter(dual, "", "codebuddy")).toBe(true);
    expect(matchesFilter(single, "", "codebuddy")).toBe(false);
    expect(matchesFilter(single, "", "trae")).toBe(true);
  });

  it("搜索大小写不敏感；空查询不过滤", () => {
    expect(matchesFilter(dual, "GLM", "all")).toBe(true);
    expect(matchesFilter(dual, "kimi", "all")).toBe(false);
    expect(matchesFilter(dual, "  ", "all")).toBe(true);
  });

  it("也按人类可读名搜索（Qoder 代号 id + 可读名）", () => {
    const qoder = pickable({ id: "qmodel_38max", name: "Qwen3.8-Max", providers: ["qoder"] });
    expect(matchesFilter(qoder, "qwen", "all")).toBe(true);      // 按 name 命中
    expect(matchesFilter(qoder, "38max", "all")).toBe(true);     // 按 id 命中
    expect(matchesFilter(qoder, "deepseek", "all")).toBe(false);
  });
});

describe("displayName", () => {
  it("优先上游人类可读名；缺失或空白时回退 id", () => {
    expect(displayName({ id: "qmodel_38max", name: "Qwen3.8-Max" })).toBe("Qwen3.8-Max");
    expect(displayName({ id: "dmodel" })).toBe("dmodel");
    expect(displayName({ id: "dmodel", name: "   " })).toBe("dmodel");
  });
});