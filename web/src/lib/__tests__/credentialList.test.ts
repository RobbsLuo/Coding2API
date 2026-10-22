import { describe, expect, it } from "vitest";
import { makeCredential } from "@/test-utils";
import {
  ANY,
  DEFAULT_FILTERS,
  DEFAULT_GROUP_SORT,
  filterCredentials,
  groupByProvider,
  matchesFilters,
  providerLedger,
  providerOptions,
  sortGroups,
  stateOptions,
  tagOptions,
} from "../credentialList";
import type { Credential } from "../../api/types";
import type { ProviderGroup } from "../credentialList";

function credential(overrides: Record<string, unknown> = {}): Credential {
  return makeCredential(overrides) as Credential;
}

const NOW = 1_700_000_000;

describe("matchesFilters", () => {
  it("默认筛选放行全部", () => {
    expect(matchesFilters(credential(), DEFAULT_FILTERS, NOW)).toBe(true);
  });

  it("渠道筛选只留该渠道", () => {
    const filters = { ...DEFAULT_FILTERS, provider: "trae" };
    expect(matchesFilters(credential({ provider: "trae" }), filters, NOW)).toBe(true);
    expect(matchesFilters(credential({ provider: "codebuddy" }), filters, NOW)).toBe(false);
  });

  it("状态筛选复用 credentialState（与徽章同口径）", () => {
    const cooling = credential({ cooling_until: NOW + 600 });
    const filters = { ...DEFAULT_FILTERS, state: "cooling" };
    expect(matchesFilters(cooling, filters, NOW)).toBe(true);
    expect(matchesFilters(credential({ cooling_until: null }), filters, NOW)).toBe(false);
  });

  it("搜索匹配昵称 / 凭证 id / 渠道展示名，忽略大小写与首尾空白", () => {
    const item = credential({ id: "cred_abc", nickname: "主账号", provider: "codebuddy" });
    for (const query of ["主账号", "CRED_ABC", " codebuddy ", "CodeBuddy"]) {
      expect(matchesFilters(item, { ...DEFAULT_FILTERS, query }, NOW)).toBe(true);
    }
    expect(matchesFilters(item, { ...DEFAULT_FILTERS, query: "zzz" }, NOW)).toBe(false);
  });

  it("昵称为空时只按 id / 渠道名匹配，不因 null 崩", () => {
    const item = credential({ id: "cred_x", nickname: "", provider: "trae" });
    expect(matchesFilters(item, { ...DEFAULT_FILTERS, query: "cred_x" }, NOW)).toBe(true);
    expect(matchesFilters(item, { ...DEFAULT_FILTERS, query: "trae" }, NOW)).toBe(true);
  });

  it("多个条件是 AND 关系", () => {
    const item = credential({ provider: "trae", nickname: "备用" });
    expect(
      matchesFilters(item, { ...DEFAULT_FILTERS, query: "备用", provider: "trae", state: "ready" }, NOW),
    ).toBe(true);
    expect(
      matchesFilters(item, { ...DEFAULT_FILTERS, query: "备用", provider: "codebuddy", state: "ready" }, NOW),
    ).toBe(false);
  });

  it("标签筛选精确匹配（大小写不敏感），且不参与搜索词", () => {
    const item = credential({ id: "cred_t", nickname: "主账号", tags: ["Prod", "备用"] });
    expect(matchesFilters(item, { ...DEFAULT_FILTERS, tag: "prod" }, NOW)).toBe(true);
    expect(matchesFilters(item, { ...DEFAULT_FILTERS, tag: " 备用 " }, NOW)).toBe(true);
    expect(matchesFilters(item, { ...DEFAULT_FILTERS, tag: "生" }, NOW)).toBe(false);
    // 无标签凭证在任一标签筛选下都被剔除
    expect(matchesFilters(credential({ tags: [] }), { ...DEFAULT_FILTERS, tag: "prod" }, NOW))
      .toBe(false);
    // 标签是结构化维度：不该被搜索词命中（否则「搜昵称叫『生产』的号」变得不可能）
    expect(matchesFilters(item, { ...DEFAULT_FILTERS, query: "prod" }, NOW)).toBe(false);
  });

  it("tags 缺失（老后端）时不因 undefined 崩，按无标签处理", () => {
    const legacy = { ...credential(), tags: undefined } as unknown as Credential;
    expect(matchesFilters(legacy, DEFAULT_FILTERS, NOW)).toBe(true);
    expect(matchesFilters(legacy, { ...DEFAULT_FILTERS, tag: "prod" }, NOW)).toBe(false);
  });
});

describe("filterCredentials", () => {
  it("保持原顺序，只做剔除", () => {
    const items = [
      credential({ id: "a", provider: "trae" }),
      credential({ id: "b", provider: "codebuddy" }),
      credential({ id: "c", provider: "trae" }),
    ];
    const result = filterCredentials(items, { ...DEFAULT_FILTERS, provider: "trae" }, NOW);
    expect(result.map((item) => item.id)).toEqual(["a", "c"]);
  });
});

describe("groupByProvider", () => {
  it("组按全站渠道顺序排（CodeBuddy / TRAE 在前），未知渠道垫底", () => {
    const groups = groupByProvider([
      credential({ id: "z", provider: "zen" }),
      credential({ id: "t", provider: "trae" }),
      credential({ id: "c", provider: "codebuddy" }),
      credential({ id: "u", provider: "mystery" as never }),
    ]);
    expect(groups.map((group) => group.key)).toEqual([
      "codebuddy",
      "trae",
      "zen",
      "mystery",
    ]);
  });

  it("CodeArts 只有一个组（不再拆内置/福利），账本为 credits", () => {
    const groups = groupByProvider([
      credential({ id: "ca1", provider: "codearts" }),
      credential({ id: "ca2", provider: "codearts" }),
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0]).toMatchObject({
      key: "codearts",
      provider: "codearts",
      label: "CodeArts",
      ledger: "credits",
    });
    // 一个真实渠道只有一个组：账号只出现一次
    expect(groups[0].items.map((item) => item.id)).toEqual(["ca1", "ca2"]);
    expect(groups[0].states).toEqual({ ready: 2 });
  });

  it("非 CodeArts 渠道只有一组，label 用全站展示名", () => {
    const groups = groupByProvider([credential({ id: "cb", provider: "codebuddy" })]);
    expect(groups).toHaveLength(1);
    expect(groups[0]).toMatchObject({
      key: "codebuddy", provider: "codebuddy", label: "CodeBuddy", ledger: "pool",
    });
  });

  it("组内保持传入顺序（不改变后端排序语义）", () => {
    const groups = groupByProvider([
      credential({ id: "first", provider: "trae" }),
      credential({ id: "second", provider: "trae" }),
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].items.map((item) => item.id)).toEqual(["first", "second"]);
  });

  it("空列表给出空分组", () => {
    expect(groupByProvider([])).toEqual([]);
  });

  it("组头聚合状态分布 / 额度 / 到期积分，且保持 null 语义", () => {
    const groups = groupByProvider(
      [
        credential({
          id: "ready",
          provider: "trae",
          quota_remaining: 60,
          quota_total: 100,
          quota_expiring_credits: 10,
          quota_expiring_credits_secondary: 25,
        }),
        credential({
          id: "cooling",
          provider: "trae",
          cooling_until: NOW + 600,
          quota_remaining: 40,
          quota_total: 100,
          quota_expiring_credits: 5,
          quota_expiring_credits_secondary: 10,
        }),
      ],
      NOW,
    );
    expect(groups).toHaveLength(1);
    const group = groups[0];
    expect(group.states).toEqual({ ready: 1, cooling: 1 });
    expect(group.remaining).toBe(100);
    expect(group.total).toBe(200);
    expect(group.expiring).toBe(15);
    expect(group.expiringSecondary).toBe(35);
  });

  it("免费层（额度 / 到期积分恒 null）聚合后仍是 null，不会当成 0", () => {
    const group = groupByProvider(
      [
        credential({ id: "zen1", provider: "zen", quota_remaining: null, quota_total: null }),
        credential({
          id: "zen2",
          provider: "zen",
          quota_remaining: null,
          quota_total: null,
          quota_expiring_credits: null,
          quota_expiring_credits_secondary: null,
        }),
      ],
      NOW,
    )[0];
    expect(group.remaining).toBeNull();
    expect(group.total).toBeNull();
    expect(group.expiring).toBeNull();
    expect(group.expiringSecondary).toBeNull();
  });

  it("部分为 null 时只对非 null 求和（不把 null 当 0 摊薄）", () => {
    const group = groupByProvider(
      [
        credential({ id: "a", provider: "trae", quota_remaining: 30, quota_total: 100 }),
        credential({ id: "b", provider: "trae", quota_remaining: null, quota_total: null }),
      ],
      NOW,
    )[0];
    expect(group.remaining).toBe(30);
    expect(group.total).toBe(100);
  });

  it("CodeArts 组按通用额度字段聚合到期积分，账本为 credits", () => {
    const groups = groupByProvider(
      [
        credential({
          id: "ca1", provider: "codearts",
          quota_expiring_credits: 100,
          quota_expiring_credits_secondary: 140,
        }),
        credential({
          id: "ca2", provider: "codearts",
          quota_expiring_credits: 50,
          quota_expiring_credits_secondary: 60,
        }),
      ],
      NOW,
    );
    // 唯一的 CodeArts 组：主/次窗口都按通用字段求和
    expect(groups).toHaveLength(1);
    expect(groups[0].ledger).toBe("credits");
    expect(groups[0].expiring).toBe(150);
    expect(groups[0].expiringSecondary).toBe(200);
  });
});

describe("providerLedger", () => {
  it("CodeArts 记积分账，其余渠道（含未知）记套餐池账", () => {
    expect(providerLedger("codearts")).toBe("credits");
    for (const provider of [
      "codebuddy", "codebuddy_intl", "trae", "zen", "kilo", "qoder", "officeace", "mystery",
    ]) {
      expect(providerLedger(provider)).toBe("pool");
    }
  });
});

describe("sortGroups", () => {
  const groups = (): ProviderGroup[] => [
    { key: "codebuddy", provider: "codebuddy", label: "CodeBuddy",
      ledger: "pool", items: [], states: {}, remaining: 50, total: 100,
      expiring: null, expiringSecondary: null },
    { key: "trae", provider: "trae", label: "TRAE",
      ledger: "pool", items: [], states: {}, remaining: 900, total: 1000,
      expiring: null, expiringSecondary: null },
    { key: "zen", provider: "zen", label: "OpenCode Zen",
      ledger: "pool", items: [], states: {}, remaining: null, total: null,
      expiring: null, expiringSecondary: null },
  ];

  it("provider 口径原样返回（groupByProvider 已排好）", () => {
    const input = groups();
    expect(sortGroups(input, "provider")).toBe(input);
  });

  it("默认口径是额度从高到低", () => {
    expect(DEFAULT_GROUP_SORT).toBe("quota");
  });

  it("quota 口径按额度合计从高到低", () => {
    expect(sortGroups(groups(), "quota").map((group) => group.key)).toEqual([
      "trae",
      "codebuddy",
      "zen",
    ]);
  });

  it("无额度接口（remaining null）垫底，不当 0 参与比较", () => {
    const sorted = sortGroups(
      [
        { key: "zen", provider: "zen", label: "OpenCode Zen",
          ledger: "pool", items: [], states: {}, remaining: null, total: null,
          expiring: null, expiringSecondary: null },
        { key: "codebuddy", provider: "codebuddy", label: "CodeBuddy",
          ledger: "pool", items: [], states: {}, remaining: 0, total: 100,
          expiring: null, expiringSecondary: null },
      ] as ProviderGroup[],
      "quota",
    );
    // 真的剩 0 的渠道排在「没有额度接口」之前
    expect(sorted.map((group) => group.key)).toEqual(["codebuddy", "zen"]);
  });

  it("额度相同（含都为 null）时回落到渠道顺序，不抖动", () => {
    const sorted = sortGroups(
      [
        { key: "zen", provider: "zen", label: "OpenCode Zen",
          ledger: "pool", items: [], states: {}, remaining: null, total: null,
          expiring: null, expiringSecondary: null },
        { key: "kilo", provider: "kilo", label: "Kilo",
          ledger: "pool", items: [], states: {}, remaining: null, total: null,
          expiring: null, expiringSecondary: null },
      ] as ProviderGroup[],
      "quota",
    );
    // providerRank 里 zen 在 kilo 前
    expect(sorted.map((group) => group.key)).toEqual(["zen", "kilo"]);
  });

  it("额度相同时按分组 key 兜底定序，不随入参抖动", () => {
    const sorted = sortGroups(
      [
        { key: "mystery-b", provider: "mystery-b", label: "mystery-b",
          ledger: "pool", items: [], states: {}, remaining: 100,
          total: 1000, expiring: null, expiringSecondary: null },
        { key: "mystery-a", provider: "mystery-a", label: "mystery-a",
          ledger: "pool", items: [], states: {}, remaining: 100,
          total: 1000, expiring: null, expiringSecondary: null },
      ] as ProviderGroup[],
      "quota",
    );
    // 两个未知渠道同权重、同额度 → 回落到 key 字典序
    expect(sorted.map((group) => group.key)).toEqual(["mystery-a", "mystery-b"]);
  });

  it("不改动入参数组的顺序", () => {
    const input = groups();
    const before = input.map((group) => group.key);
    sortGroups(input, "quota");
    expect(input.map((group) => group.key)).toEqual(before);
  });
});

describe("providerOptions", () => {
  it("只列出现过的渠道并带计数，顺序与全站一致", () => {
    const options = providerOptions([
      credential({ provider: "zen" }),
      credential({ provider: "trae" }),
      credential({ provider: "trae" }),
      credential({ provider: "codebuddy" }),
    ]);
    expect(options).toEqual([
      { value: "codebuddy", label: "CodeBuddy", count: 1 },
      { value: "trae", label: "TRAE", count: 2 },
      { value: "zen", label: "OpenCode Zen", count: 1 },
    ]);
  });

  it("空池没有渠道项", () => {
    expect(providerOptions([])).toEqual([]);
  });
});

describe("stateOptions", () => {
  it("按语义顺序列出有计数的状态", () => {
    const options = stateOptions(
      [
        credential({ id: "a", health: -1 }),
        credential({ id: "b", cooling_until: NOW + 60 }),
        credential({ id: "c" }),
        credential({ id: "d" }),
      ],
      NOW,
    );
    expect(options).toEqual([
      { value: "ready", label: "可用", count: 2 },
      { value: "cooling", label: "冷却中", count: 1 },
      { value: "exhausted", label: "额度耗尽", count: 1 },
    ]);
  });

  it("零计数的状态不占位（避免一排 0 制造噪声）", () => {
    const options = stateOptions([credential({ id: "a" })], NOW);
    expect(options.map((option) => option.value)).toEqual(["ready"]);
  });

  it("当前选中的零计数状态保留，否则选中态会无处可归", () => {
    const options = stateOptions([credential({ id: "a" })], NOW, "disabled");
    expect(options.map((option) => option.value)).toEqual(["ready", "disabled"]);
    expect(options[1]).toEqual({ value: "disabled", label: "已禁用", count: 0 });
  });

  it("ANY 不是具体状态，不会被当成选中项保留", () => {
    const options = stateOptions([credential({ id: "a" })], NOW, ANY);
    expect(options.map((option) => option.value)).toEqual(["ready"]);
  });
});

describe("tagOptions", () => {
  it("按首次出现顺序列出标签与计数，大小写不敏感合并", () => {
    const options = tagOptions([
      credential({ id: "a", tags: ["生产", "prod"] }),
      credential({ id: "b", tags: ["prod", "PROD"] }),
      credential({ id: "c", tags: [] }),
    ]);
    // prod / PROD 合并成一个，标签取首次写法
    expect(options).toEqual([
      { value: "生产", label: "生产", count: 1 },
      { value: "prod", label: "prod", count: 2 },
    ]);
  });

  it("无标签的池没有标签项", () => {
    expect(tagOptions([credential({ tags: [] })])).toEqual([]);
    expect(tagOptions([])).toEqual([]);
  });

  it("老后端缺 tags 字段（undefined）不崩", () => {
    const legacy = { ...credential(), tags: undefined } as unknown as Credential;
    expect(tagOptions([legacy])).toEqual([]);
  });
});
