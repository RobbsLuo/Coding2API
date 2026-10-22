import { describe, expect, it } from "vitest";
import type { ModelInfo } from "../../api/types";
import {
  buildSections,
  displayName,
  filterOptions,
  groupModelsByProvider,
  matchesFilter,
  modelRate,
  modelValue,
  pickDefaultModel,
  providerRate,
  rateLabel,
  rateSummary,
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

  it("也按展示名搜索（后端已清洗：Qwen3.8-Max → Qwen3.8 Max）", () => {
    const qoder = pickable({ id: "qmodel_38max", name: "Qwen3.8 Max", providers: ["qoder"] });
    expect(matchesFilter(qoder, "qwen", "all")).toBe(true);      // 按 name 命中
    expect(matchesFilter(qoder, "38max", "all")).toBe(true);     // 按 id 命中
    expect(matchesFilter(qoder, "deepseek", "all")).toBe(false);
  });

  it("也按各渠道原 key 搜索（对外 id 与实际请求 key 不同）", () => {
    // 多渠道条目对外 id 是展示名的归一键，与各渠道实际发的 key 不同
    const merged = pickable({
      id: "longcat-2.5-preview",
      name: "LongCat 2.5 Preview",
      providers: ["zen", "trae"],
      by_provider: {
        zen: { raw_id: "longcat-2.5-preview-free" },
        trae: { raw_id: "longcat-2.5-preview" },
      },
    });
    expect(matchesFilter(merged, "-free", "all")).toBe(true);    // 按 zen 的原 key 命中
    expect(matchesFilter(merged, "longcat", "all")).toBe(true);  // 按对外 id 命中
    expect(matchesFilter(merged, "mimo", "all")).toBe(false);
    // 无 by_provider 的条目按 id/name 匹配，不因缺 raw_id 而报错
    expect(matchesFilter(pickable({ id: "kilo-auto/free", providers: ["kilo"] }), "kilo", "all")).toBe(true);
  });
});

describe("displayName", () => {
  it("直接用后端清洗过的展示名；缺失或空白时回退 id", () => {
    expect(displayName({ id: "qmodel_38max", name: "Qwen3.8 Max" })).toBe("Qwen3.8 Max");
    expect(displayName({ id: "dmodel" })).toBe("dmodel");
    expect(displayName({ id: "dmodel", name: "   " })).toBe("dmodel");
  });
});

describe("groupModelsByProvider", () => {
  it("多渠道条目同时进各渠道，倍率按渠道取（不拿合并值冒充）", () => {
    const grouped = groupModelsByProvider([
      model({
        id: "glm-5.2", name: "GLM 5.2", providers: ["codebuddy", "trae"],
        // 合并值是 0.29，但两个渠道各自不同；多渠道时不能用合并值
        credit_rate: 0.29,
        by_provider: { codebuddy: { credit_rate: 0.29 }, trae: { credit_rate: 0.5 } },
      }),
      model({ id: "kimi-k3", name: "Kimi K3", providers: ["trae"], credit_rate: 0 }),
      model({ id: "aaa", providers: ["trae"] }),
    ]);
    expect([...grouped.keys()].sort()).toEqual(["codebuddy", "trae"]);
    expect(grouped.get("codebuddy")).toEqual([
      { id: "glm-5.2", label: "GLM 5.2", rate: 0.29 },
    ]);
    // 展示名排序（zh-CN 排序忽略大小写）：aaa < GLM 5.2 < Kimi K3
    expect(grouped.get("trae")).toEqual([
      // 无倍率：rate 为 undefined（展示 —），不是 0
      { id: "aaa", label: "aaa", rate: undefined },
      { id: "glm-5.2", label: "GLM 5.2", rate: 0.5 },
      { id: "kimi-k3", label: "Kimi K3", rate: 0 },
    ]);
  });

  it("单渠道模型回退合并倍率；同名条目按 id 稳定排序；空列表返回空 Map", () => {
    const grouped = groupModelsByProvider([
      model({ id: "b", name: "同名", providers: ["trae"], credit_rate: 0.1 }),
      model({ id: "a", name: "同名", providers: ["trae"], credit_rate: 0.2 }),
    ]);
    expect(grouped.get("trae")?.map((item) => item.id)).toEqual(["a", "b"]);
    expect(grouped.get("trae")?.map((item) => item.rate)).toEqual([0.2, 0.1]);
    expect(groupModelsByProvider([]).size).toBe(0);
  });

  it("自定义别名不算独立模型（后端克隆目标条目，算进来会虚高）", () => {
    const grouped = groupModelsByProvider([
      model({ id: "kimi-k3", name: "Kimi K3", providers: ["trae"], credit_rate: 0.5 }),
      model({
        id: "my-kimi", name: "my-kimi", providers: ["trae"], credit_rate: 0.5,
        custom_alias: true, alias_target: "kimi-k3",
      } as Partial<ModelInfo> & Pick<ModelInfo, "id" | "providers">),
    ]);
    expect(grouped.get("trae")).toEqual([
      { id: "kimi-k3", label: "Kimi K3", rate: 0.5 },
    ]);
  });
});

describe("rateSummary", () => {
  it("免费额度不参与取最低：混着免费模型时不显示「最低 免」", () => {
    expect(rateSummary([
      { id: "a", label: "A", rate: 0 },
      { id: "b", label: "B", rate: 0.05 },
      { id: "c", label: "C", rate: 2 },
    ])).toBe("最低 x0.05");
  });

  it("整组都免费时说「全免」", () => {
    expect(rateSummary([
      { id: "a", label: "A", rate: 0 },
      { id: "b", label: "B", rate: 0 },
    ])).toBe("全免");
  });

  it("一条倍率都没有（或没有模型）时不造「最低」", () => {
    expect(rateSummary([{ id: "a", label: "A" }])).toBeNull();
    expect(rateSummary([])).toBeNull();
  });
});

describe("rateLabel", () => {
  it("0 是免费额度（免），其余加 x 前缀", () => {
    expect(rateLabel(0)).toBe("免");
    expect(rateLabel(0.29)).toBe("x0.29");
    expect(rateLabel(2)).toBe("x2");
  });

  it("抹掉浮点长尾，也不把极小倍率压成 0（那会显示成「免」）", () => {
    expect(rateLabel(0.1 + 0.2)).toBe("x0.3");
    expect(rateLabel(0.004)).toBe("x0.004");
  });
});