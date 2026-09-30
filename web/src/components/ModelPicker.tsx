import { useMemo, useState } from "react";
import { Check, RefreshCw, Search } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ModelInfo } from "../api/types";
import { providerChartColor, providerLabel, providerRank } from "../api/providers";
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

/** 单条是否通过搜索 + 渠道筛选（"all" 不限；"auto" 只留多渠道）。
 *
 * 搜索同时匹配 id（`qmodel_38max`）与人类可读名（`Qwen3.8-Max`），
 * 用户按任一种都能找到模型。
 */
export function matchesFilter(item: PickableModel, query: string, filter: string): boolean {
  if (filter === "auto" && item.providers.length < 2) return false;
  if (filter !== "all" && filter !== "auto" && !(item.providers as string[]).includes(filter)) {
    return false;
  }
  const needle = query.trim().toLowerCase();
  return needle === ""
    || item.id.toLowerCase().includes(needle)
    || (item.name ?? "").toLowerCase().includes(needle);
}

/** 展示用主名：优上游人类可读名，缺失时回退内部 id。 */
export function displayName(item: { id: string; name?: string }): string {
  const name = (item.name ?? "").trim();
  return name === "" ? item.id : name;
}

/** 渠道徽章的展示顺序（CB → TR → 其余），与全站展示顺序一致。 */
function sortProviders(providers: string[]): string[] {
  return [...providers].sort((left, right) => providerRank(left) - providerRank(right));
}

/**
 * 品牌着色渠道徽章：底/边/字都取自该渠道的图表主色，渠道一眼可辨。
 *
 * 倍率作为其内的加粗数字块单独强调（`x0.29`；免费额度显示「免费」）。
 */
function ChannelPill({ provider, rate }: { provider: string; rate?: number }) {
  const color = providerChartColor(provider);
  return (
    <span
      data-testid={`channel-pill-${provider}`}
      className="inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11px] font-semibold"
      style={{
        borderColor: `color-mix(in oklch, ${color} 45%, transparent)`,
        backgroundColor: `color-mix(in oklch, ${color} 14%, transparent)`,
        color,
      }}
    >
      <ProviderIcon provider={provider} size={13} />
      {providerLabel(provider)}
      {rate !== undefined && (
        <span className="rounded bg-background/70 px-1 font-bold tabular-nums">
          {rate === 0 ? "免费" : `x${rate}`}
        </span>
      )}
    </span>
  );
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
  const providers = sortProviders(item.providers);
  const multi = item.providers.length > 1;
  return (
    <button
      type="button"
      role="option"
      aria-selected={selected}
      data-testid={`model-option-${item.value}`}
      onClick={onSelect}
      className={cn(
        "flex w-full flex-col gap-1.5 rounded-xl border px-3 py-2 text-left transition-colors",
        selected
          ? "border-primary/60 bg-primary/[0.08] ring-1 ring-primary/30"
          : "border-border bg-card hover:border-primary/30 hover:bg-muted/50",
      )}
    >
      <span className="flex items-center gap-2">
        <span
          className={cn(
            "flex size-4 shrink-0 items-center justify-center rounded-full",
            selected ? "bg-primary text-primary-foreground" : "border border-border",
          )}
        >
          {selected && <Check className="size-3" />}
        </span>
        <span className="min-w-0 flex-1">
          {/* 主名用上游人类可读名（Qwen3.8-Max）；与内部 id 不同时补一行小字 id，
              否则用户无法用 id 直连指定。 */}
          <span className="block truncate text-sm font-semibold">{displayName(item)}</span>
          {displayName(item) !== item.id && (
            <span className="block truncate font-mono text-[11px] text-muted-foreground">
              {item.id}
            </span>
          )}
        </span>
        {multi && (
          <span className="shrink-0 rounded-md bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
            自动调度
          </span>
        )}
      </span>
      <span className="flex flex-wrap items-center gap-1.5 pl-6">
        {providers.map((provider) => (
          <ChannelPill key={provider} provider={provider} rate={providerRate(item, provider)} />
        ))}
      </span>
    </button>
  );
}

/**
 * 可视化模型选择器：搜索框 + 渠道筛选 chips + 品牌着色的模型卡片列表。
 *
 * 每行展示模型名与该模型可用的**全部渠道徽章**及各自倍率，多渠道模型因此
 * 一眼可辨；单渠道模型归入「仅 XX」分组。顶部常驻「当前选择」条，滚动时
 * 也能看清选中项。选中值语义见 `modelValue`。
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
  const pinnedProvider = value.includes("@") ? value.split("@")[1] : "";
  const selectedItem =
    models.find((item) => item.value === value) ??
    models.find((item) => item.id === baseValue);

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
                  ? "border-primary bg-primary text-primary-foreground"
                  : "border-border text-muted-foreground hover:border-primary/40 hover:text-foreground",
              )}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>

      {selectedItem && (
        <div
          data-testid="model-current"
          className="flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-lg border border-primary/40 bg-primary/[0.06] px-3 py-2"
        >
          <span className="text-[11px] font-medium text-muted-foreground">当前选择</span>
          <span className="flex flex-col">
            <span className="text-sm font-semibold">{displayName(selectedItem)}</span>
            {displayName(selectedItem) !== selectedItem.id && (
              <span className="font-mono text-[11px] text-muted-foreground">
                {selectedItem.id}
              </span>
            )}
          </span>
          {sortProviders(selectedItem.providers).map((provider) => (
            <ChannelPill
              key={provider}
              provider={provider}
              rate={providerRate(selectedItem, provider)}
            />
          ))}
          {pinnedProvider && (
            <span className="rounded-md bg-primary/15 px-1.5 py-0.5 text-[11px] font-semibold text-primary">
              已强制 {providerLabel(pinnedProvider)}
            </span>
          )}
          {!pinnedProvider && selectedItem.providers.length > 1 && (
            <span className="rounded-md bg-muted px-1.5 py-0.5 text-[11px] font-medium text-muted-foreground">
              自动路由
            </span>
          )}
        </div>
      )}

      <div
        role="listbox"
        data-testid="model-list"
        className="max-h-80 space-y-4 overflow-y-auto pr-1"
      >
        {sections.length === 0 ? (
          <p className="py-6 text-center text-sm text-muted-foreground">没有匹配的模型</p>
        ) : (
          sections.map((section) => (
            <div key={section.key}>
              <div className="mb-1.5 flex items-center gap-2">
                <span className="h-3.5 w-1 rounded-full bg-primary/60" />
                <span
                  data-testid={`model-section-${section.key}`}
                  className="text-xs font-semibold text-foreground"
                >
                  {section.label}
                </span>
                <span className="text-[11px] text-muted-foreground">
                  {section.items.length} 个
                </span>
              </div>
              <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
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