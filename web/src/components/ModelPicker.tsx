import { useMemo, useState } from "react";
import { Check, RefreshCw, Search } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ModelBenchmarks, ModelInfo } from "../api/types";
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

/** 该渠道实际请求的 key（raw key）：展示在渠道徽章的 tooltip 上。
 *
 * 对外 id 是归一后的（多渠道条目甚至会用展示名），而转发时发的是各渠道自己
 * 的原 key（`kilo-auto/free` / `kmodel_latest`）——排障时需要看见后者。
 */
function providerRawId(item: ModelInfo, provider: string): string | undefined {
  return item.by_provider?.[provider]?.raw_id;
}

/**
 * 单条是否通过搜索 + 渠道筛选（"all" 不限；"auto" 只留多渠道）。
 *
 * 搜索同时匹配对外 id（`kimi-k3`）、展示名（`Kimi K3`）与各渠道原 key
 * （`kilo-auto/free` / `kmodel_latest`），用户按任一种都能找到模型。
 */
export function matchesFilter(item: PickableModel, query: string, filter: string): boolean {
  if (filter === "auto" && item.providers.length < 2) return false;
  if (filter !== "all" && filter !== "auto" && !(item.providers as string[]).includes(filter)) {
    return false;
  }
  const needle = query.trim().toLowerCase();
  if (needle === "") return true;
  if (item.id.toLowerCase().includes(needle)) return true;
  if ((item.name ?? "").toLowerCase().includes(needle)) return true;
  return item.providers.some((provider) =>
    (providerRawId(item, provider) ?? "").toLowerCase().includes(needle));
}

/**
 * 展示用主名：后端已统一清洗过的 `name`，缺失时回退内部 id。
 *
 * 清洗在后端做（`src/provider/naming.py`），前端不再按渠道猜怎么美化——
 * 六条渠道同一口径，`Kimi K3` / `LongCat 2.5 Preview` 都直接可展示。
 */
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
 * `rawId` 非空时作为 tooltip：该渠道实际请求的 key。
 */
function ChannelPill({
  provider,
  rate,
  rawId,
}: {
  provider: string;
  rate?: number;
  rawId?: string;
}) {
  const color = providerChartColor(provider);
  return (
    <span
      data-testid={`channel-pill-${provider}`}
      title={rawId}
      className="inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11px] font-semibold"
      style={{
        borderColor: `color-mix(in oklch, ${color} 45%, transparent)`,
        backgroundColor: `color-mix(in oklch, ${color} 14%, transparent)`,
        // 文字往 ink 混 25%：纯渠道色在自身浅底上对比不足（≈3.1~4.4）
        color: `color-mix(in oklch, ${color} 75%, var(--ink))`,
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

/**
 * 能力分 tooltip 文本：三项指数 + 来源说明。
 *
 * 必须写明「第三方成绩、非本服务实测」：用户容易把这里的分数当成 coding2api
 * 自己跑出来的排名。上游只给部分项时只列有的那几项。
 */
function benchmarkTitle(item: { benchmarks?: ModelBenchmarks }): string {
  const b = item.benchmarks;
  if (!b) return "";
  const parts: string[] = [];
  if (b.intelligence_index !== undefined) parts.push(`智能 ${b.intelligence_index}`);
  if (b.coding_index !== undefined) parts.push(`编程 ${b.coding_index}`);
  if (b.agentic_index !== undefined) parts.push(`智能体 ${b.agentic_index}`);
  const source = b.source_model ? `（${b.source_model}）` : "";
  return `${parts.join(" · ")}\nArtificial Analysis 指数，经 OpenRouter 公开接口${source}；非本服务实测`;
}

/**
 * 能力分行内徽章：只显示**综合智能指数**（三项全列会把行挤爆），无分不渲染。
 *
 * 颜色分档（前端本地口径，只用于让强模型在列表里更显眼）：≥40 主色、
 * ≥30 中性、其余弱化。后端不返回任何排序，这里也不排——展示层不做排名。
 */
function BenchmarkBadge({ item }: { item: { benchmarks?: ModelBenchmarks } }) {
  const score = item.benchmarks?.intelligence_index;
  if (score === undefined) return null;
  const tone = score >= 40
    ? "border-primary/40 bg-primary/10 text-primary-ink"
    : score >= 30
      ? "border-border bg-muted text-foreground"
      : "border-border text-muted-foreground";
  return (
    <span
      data-testid="model-benchmark"
      title={benchmarkTitle(item)}
      className={cn(
        "inline-flex items-center gap-0.5 rounded-md border px-1.5 py-0.5 text-[11px] font-semibold tabular-nums",
        tone,
      )}
    >
      智 {score}
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
  return (
    <button
      type="button"
      role="option"
      aria-selected={selected}
      data-testid={`model-option-${item.value}`}
      onClick={onSelect}
      className={cn(
        "flex w-full items-center gap-2 rounded-xl border px-3 py-2 text-left transition-colors",
        selected
          ? "border-primary/60 bg-primary/[0.08] ring-1 ring-primary/30"
          : "border-border bg-card hover:border-primary/30 hover:bg-muted/50",
      )}
    >
      <span
        className={cn(
          "flex size-4 shrink-0 items-center justify-center rounded-full",
          selected ? "bg-primary text-primary-foreground" : "border border-border",
        )}
      >
        {selected && <Check className="size-3" />}
      </span>
      {/* 名称占优：`min-w-40`（10rem）给名称留出最低宽度，徽章再多也不会把名称
          挤成省略号（`deepseek-v4.1-flash` 有四条渠道，曾被挤到 0px）。名称占
          满剩余空间，`flex-1` 保证单/双渠道行名称仍很宽。 */}
      <span className="min-w-40 flex-1" title={displayName(item)}>
        {/* 主名用上游人类可读名（Qwen3.8-Max）；与内部 id 不同时补一行小字 id，
            否则用户无法用 id 直连指定。 */}
        <span className="block truncate text-sm font-semibold">{displayName(item)}</span>
        {displayName(item) !== item.id && (
          <span className="block truncate font-mono text-[11px] text-muted-foreground">
            {item.id}
          </span>
        )}
      </span>
      {/* 渠道 + 倍率常驻右侧：名称 `flex-1` 占满左侧后徽章组自然贴右；徽章组
          自身 `flex-wrap`，多渠道放不下时**在右侧区域内折行**，而不是整组掉到
          名称下方独占一行（内层 `justify-end` 让每行都贴右）。 */}
      <span className="flex min-w-0 flex-wrap items-center justify-end gap-1.5">
        <BenchmarkBadge item={item} />
        {providers.map((provider) => (
          <ChannelPill
            key={provider}
            provider={provider}
            rate={providerRate(item, provider)}
            rawId={providerRawId(item, provider)}
          />
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
  onPin,
  loading,
}: {
  models: PickableModel[];
  value: string;
  onChange: (value: string) => void;
  /** 多渠道模型在「当前选择」条里强制指定渠道；不传则不显示该控件。 */
  onPin?: (value: string) => void;
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
          <span className="flex flex-wrap items-center gap-1.5">
            <BenchmarkBadge item={selectedItem} />
            {sortProviders(selectedItem.providers).map((provider) => (
              <ChannelPill
                key={provider}
                provider={provider}
                rate={providerRate(selectedItem, provider)}
                rawId={providerRawId(selectedItem, provider)}
              />
            ))}
          </span>
          {/* 右侧统一区：多渠道放渠道指定控件，已强制时追加「已强制 XX」标；
              单渠道模型 value 天然带 @provider，也走「已强制」标显示在右侧 */}
          {((onPin && selectedItem.providers.length > 1) || pinnedProvider) && (
            <span className="ml-auto flex flex-wrap items-center justify-end gap-1.5">
              {onPin && selectedItem.providers.length > 1 && (
                <>
                  <span className="text-[11px] font-medium text-muted-foreground">渠道</span>
                  <span className="flex flex-wrap items-center gap-1" data-testid="provider-pin">
                    <button
                      type="button"
                      data-testid="provider-pin-auto"
                      aria-pressed={pinnedProvider === ""}
                      onClick={() => onPin(baseValue)}
                      className={cn(
                        "inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium transition-colors",
                        pinnedProvider === ""
                          ? "border-primary/40 bg-primary/10 text-primary-ink"
                          : "border-border text-muted-foreground hover:text-foreground",
                      )}
                    >
                      自动路由
                    </button>
                    {sortProviders(selectedItem.providers).map((provider) => (
                      <button
                        key={provider}
                        type="button"
                        data-testid={`provider-pin-${provider}`}
                        aria-pressed={pinnedProvider === provider}
                        onClick={() => onPin(`${baseValue}@${provider}`)}
                        className={cn(
                          "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium transition-colors",
                          pinnedProvider === provider
                            ? "border-primary/40 bg-primary/10 text-primary-ink"
                            : "border-border text-muted-foreground hover:text-foreground",
                        )}
                      >
                        <ProviderIcon provider={provider} size={11} />
                        {providerLabel(provider)}
                      </button>
                    ))}
                  </span>
                </>
              )}
              {pinnedProvider && (
                <span className="rounded-md bg-primary/15 px-1.5 py-0.5 text-[11px] font-semibold text-primary-ink">
                  已强制 {providerLabel(pinnedProvider)}
                </span>
              )}
            </span>
          )}
          {!pinnedProvider && !onPin && selectedItem.providers.length > 1 && (
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