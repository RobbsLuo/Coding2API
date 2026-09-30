import { useMemo, useState } from "react";
import { RefreshCw, Search } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ModelInfo } from "../api/types";
import { providerLabel, providerRank } from "../api/providers";
import { ProviderIcon } from "./ProviderIcon";
import { Input } from "../ui";

/** 列表条目：原始 ModelInfo + 受控选中值。 */
export type PickableModel = ModelInfo & { value: string };

/**
 * 受控选中值：单渠道模型带 @provider（该组选项即强制指定），
 * 多渠道模型用裸 id（走自动路由）。
 */
export function modelValue(item: { id: string; providers: string[] }): string {
  return item.providers.length === 1 ? `${item.id}@${item.providers[0]}` : item.id;
}

/**
 * 某渠道的消耗倍率。
 *
 * 优先按渠道细分（`by_provider`）；单渠道模型回退合并后的 `credit_rate`；
 * 多渠道模型若上游没给细分倍率则返回 undefined——**不能**拿合并值冒充，
 * 否则会把某个渠道的倍率错标到其他渠道上。
 */
export function providerRate(item: ModelInfo, provider: string): number | undefined {
  const perProvider = item.by_provider?.[provider]?.credit_rate;
  if (perProvider !== undefined) return perProvider;
  if (item.providers.length === 1) return item.credit_rate;
  return undefined;
}

/** 模型排序用倍率：多渠道取各渠道最小倍率；无倍率视为最大（排最后）。 */
export function modelRate(item: ModelInfo): number | undefined {
  const rates = item.providers
    .map((provider) => providerRate(item, provider))
    .filter((rate): rate is number => rate !== undefined);
  if (rates.length) return Math.min(...rates);
  return item.credit_rate;
}

/** 默认选中：倍率最小的模型；全部无倍率时回退列表第一个。 */
export function pickDefaultModel(models: PickableModel[]): string {
  let best: PickableModel | null = null;
  let bestRate = Infinity;
  for (const item of models) {
    const rate = modelRate(item);
    if (rate !== undefined && rate < bestRate) {
      bestRate = rate;
      best = item;
    }
  }
  return (best ?? models[0])?.value ?? "";
}

export interface ModelSection {
  key: string;
  label: string;
  items: PickableModel[];
}

/**
 * 分组：多渠道置顶（自动调度），单渠道按 providerRank 分渠道。
 *
 * 与后端 `/v1/models` 的展示顺序（`_PROVIDER_RANK`）对齐——分组顺序不依赖
 * 模型列表的首次出现顺序，否则列表顺序一变分组也跟着乱。
 */
export function buildSections(models: PickableModel[]): ModelSection[] {
  const sections: ModelSection[] = [];
  const multi = models.filter((item) => item.providers.length > 1);
  if (multi.length) sections.push({ key: "auto", label: "多渠道（自动调度）", items: multi });
  const singles = models.filter((item) => item.providers.length === 1);
  const providers = [...new Set(singles.map((item) => item.providers[0]))]
    .sort((left, right) => providerRank(left) - providerRank(right));
  for (const provider of providers) {
    sections.push({
      key: provider,
      label: `仅 ${providerLabel(provider)}`,
      items: singles.filter((item) => item.providers[0] === provider),
    });
  }
  return sections;
}

/** 渠道筛选 chips：全部 / 多渠道 / 列表中出现过的渠道（按展示顺序）。 */
export function filterOptions(models: PickableModel[]): { value: string; label: string }[] {
  const options = [{ value: "all", label: "全部" }];
  if (models.some((item) => item.providers.length > 1)) {
    options.push({ value: "auto", label: "多渠道" });
  }
  const providers = [...new Set(models.flatMap((item) => item.providers))]
    .sort((left, right) => providerRank(left) - providerRank(right));
  for (const provider of providers) {
    options.push({ value: provider, label: providerLabel(provider) });
  }
  return options;
}

/** 单条是否通过搜索 + 渠道筛选（"all" 不限；"auto" 只留多渠道）。 */
export function matchesFilter(item: PickableModel, query: string, filter: string): boolean {
  if (filter === "auto" && item.providers.length < 2) return false;
  if (filter !== "all" && filter !== "auto" && !(item.providers as string[]).includes(filter)) {
    return false;
  }
  const needle = query.trim().toLowerCase();
  return needle === "" || item.id.toLowerCase().includes(needle);
}

function ModelRow({
  item,
  selected,
  onSelect,
}: {
  item: PickableModel;
  selected: boolean;
  onSelect: () => void;
}) {
  // 渠道徽章顺序与全局展示顺序一致（CB → TR → 其余）
  const providers = [...item.providers].sort(
    (left, right) => providerRank(left) - providerRank(right),
  );
  return (
    <button
      type="button"
      role="option"
      aria-selected={selected}
      data-testid={`model-option-${item.value}`}
      onClick={onSelect}
      className={cn(
        "flex w-full flex-wrap items-center justify-between gap-x-3 gap-y-1 rounded-lg border px-2.5 py-1.5 text-left transition-colors",
        selected ? "border-primary/40 bg-primary/10" : "border-transparent hover:bg-muted/60",
      )}
    >
      <span className="truncate font-mono text-xs">{item.id}</span>
      <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
        {providers.map((provider) => {
          const rate = providerRate(item, provider);
          return (
            <span
              key={provider}
              className="inline-flex items-center gap-1 text-[11px] text-muted-foreground"
            >
              <ProviderIcon provider={provider} size={12} />
              {providerLabel(provider)}
              {rate !== undefined && (
                <span className="font-medium tabular-nums text-foreground">x{rate}</span>
              )}
            </span>
          );
        })}
      </span>
    </button>
  );
}

/**
 * 可视化模型选择器：搜索框 + 渠道筛选 chips + 分组列表。
 *
 * 每行展示模型名与该模型可用的**全部渠道徽章**及各自倍率，多渠道模型因此
 * 一眼可辨；单渠道模型归入「仅 XX」分组。选中值语义见 `modelValue`。
 */
export function ModelPicker({
  models,
  value,
  onChange,
  loading,
}: {
  models: PickableModel[];
  value: string;
  onChange: (value: string) => void;
  loading?: boolean;
}) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");

  const options = useMemo(() => filterOptions(models), [models]);
  const visible = useMemo(
    () => models.filter((item) => matchesFilter(item, query, filter)),
    [models, query, filter],
  );
  const sections = useMemo(() => buildSections(visible), [visible]);
  // 强制指定渠道后值为 `id@provider`（没有对应行），高亮其基础模型行，
  // 否则列表看起来像什么都没选中。
  const baseValue = value.includes("@") ? value.split("@")[0] : value;

  return (
    <div className="space-y-3" data-testid="model-picker">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-48 flex-1">
          <Search className="pointer-events-none absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={query}
            data-testid="model-search"
            placeholder="搜索模型…"
            className="pl-7"
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>
        <div className="flex flex-wrap items-center gap-1">
          {options.map((option) => (
            <button
              key={option.value}
              type="button"
              data-testid={`model-filter-${option.value}`}
              aria-pressed={filter === option.value}
              onClick={() => setFilter(option.value)}
              className={cn(
                "rounded-full border px-2.5 py-1 text-xs font-medium transition-colors",
                filter === option.value
                  ? "border-primary/40 bg-primary/10 text-primary"
                  : "border-border text-muted-foreground hover:text-foreground",
              )}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>

      <div
        role="listbox"
        data-testid="model-list"
        className="max-h-72 space-y-3 overflow-y-auto pr-1"
      >
        {sections.length === 0 ? (
          <p className="py-6 text-center text-sm text-muted-foreground">没有匹配的模型</p>
        ) : (
          sections.map((section) => (
            <div key={section.key}>
              <div
                data-testid={`model-section-${section.key}`}
                className="mb-1 text-[11px] font-medium tracking-wide text-muted-foreground"
              >
                {section.label}
              </div>
              <div className="space-y-1">
                {section.items.map((item) => (
                  <ModelRow
                    key={item.value}
                    item={item}
                    selected={item.value === baseValue || item.id === baseValue}
                    onSelect={() => onChange(item.value)}
                  />
                ))}
              </div>
            </div>
          ))
        )}
      </div>

      {loading && (
        <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
          <RefreshCw className="size-3 animate-spin" />
          载入模型中…
        </span>
      )}
    </div>
  );
}