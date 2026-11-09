import { useMemo, useState } from "react";
import { Check, RefreshCw, Search, Shuffle } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ModelBenchmarks, ModelInfo } from "../api/types";
import { aliasTitle, isCustomAlias } from "../api/modelExtras";
import { providerChartColor, providerLabel, providerRank } from "../api/providers";
import { ProviderIcon } from "./ProviderIcon";
import { Badge, Input } from "../ui";

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

/** 倍率文案：`0` 是免费额度（上游给 0 表示不计费），不能显示成 `x0`。 */
export function rateLabel(rate: number): string {
  if (rate === 0) return "免";
  // 上游倍率是浮点，可能带长尾（0.1+0.2 → 0.30000000000000004）。取 6 位有效
  // 数字够覆盖真实倍率，又不像 toFixed(2) 那样把 0.004 压成 0（会显示成「免」）。
  return `x${Number(rate.toPrecision(6))}`;
}

/** 凭证页组头「模型 N 个」用的条目：id 供排障，label 是展示名，rate 是该渠道的消耗倍率。 */
export interface ProviderModel {
  id: string;
  label: string;
  /** 该渠道下的倍率（`by_provider` 优先，单渠道回退合并值）；上游没给则为 undefined */
  rate?: number;
}

/**
 * 按渠道归类模型（凭证页渠道组头用）。
 *
 * 数据源是 `/api/playground/models`：它只含「有可用凭证且过当前黑名单」的渠道，
 * 与 `/v1/models` 同口径，所以组里「有凭证」和「有模型」不必各算一套。
 *
 * **倍率按渠道取**（复用 `providerRate`，与模型选择器同一口径）：多渠道模型
 * 各渠道倍率不同，拿合并值冒充会把某个渠道的倍率错标到别的渠道上。
 * 排序按展示名（人类可读名优先），同名再按 id 兜底——凭证页 30s 轮询，
 * 顺序必须稳定，不能随上游返回顺序跳动。
 */
export function groupModelsByProvider(models: ModelInfo[]): Map<string, ProviderModel[]> {
  const grouped = new Map<string, ProviderModel[]>();
  for (const item of models) {
    // 自定义别名是**同一模型**的另一个可请求名字（后端克隆目标条目生成），
    // 目标条目本身也在列表里；算进来会让「N 个模型」虚高、清单里同一模型出现两次。
    if (isCustomAlias(item)) continue;
    for (const provider of item.providers) {
      const entry: ProviderModel = {
        id: item.id, label: displayName(item), rate: providerRate(item, provider),
      };
      const bucket = grouped.get(provider);
      if (bucket) bucket.push(entry);
      else grouped.set(provider, [entry]);
    }
  }
  for (const bucket of grouped.values()) {
    bucket.sort((left, right) =>
      left.label.localeCompare(right.label, "zh-CN") || left.id.localeCompare(right.id));
  }
  return grouped;
}

/**
 * 组头「最低倍率」的文案（`null` = 一条倍率都没有，整块不显示）。
 *
 * **免费额度不参与取最低**：渠道里常混着 1~2 个 0 倍率模型（CodeBuddy 33 个里
 * 2 个、Qoder 11 个里 2 个），按字面取最小会显示「最低 免」，读起来像整个渠道
 * 免费——实际其余几十个都在收费。全渠道都免费时才说「全免」。
 */
export function rateSummary(models: ProviderModel[]): string | null {
  const rates = models
    .map((model) => model.rate)
    .filter((rate): rate is number => rate !== undefined);
  if (rates.length === 0) return null;
  const paid = rates.filter((rate) => rate > 0);
  if (paid.length === 0) return "全免";
  return `最低 ${rateLabel(Math.min(...paid))}`;
}

/** 渠道徽章的展示顺序（CB → TR → 其余），与全站展示顺序一致。 */
function sortProviders(providers: string[]): string[] {
  return [...providers].sort((left, right) => providerRank(left) - providerRank(right));
}

/**
 * 品牌着色渠道徽章：品牌图标保持彩色，文字/底色压低饱和，一组徽章并排时
 * 不会变成一条彩虹墙。
 *
 * 形态统一为**分体两段**：左段渠道名用品牌浅底，右段倍率用同品牌深一档的底
 * + 左侧分隔线。单渠道、多渠道、列表行都用同一种形态——倍率是独立的第二段，
 * 不再当渠道名后面的小附注，扫视时「渠道 | 倍率」的分界始终在同一位置。
 * 没有倍率数据的渠道只渲染左段（成一个普通圆角徽章）。
 *
 * **尺寸全局只有一档**：`text-[10px]` / `leading-4`、上下不加 padding、左右
 * `px-1.5`，成稿高 18px（含 1px 边框）。列表行、当前选择条、单/多渠道都一致；
 * 宽度才按渠道名自适应。原先列表行用 11px、选择条用 10px 两档，同一页上下
 * 两处大小不一，且与旁边的「智 xx」能力分徽章（10px）也不齐。
 *
 * 选中态由 `active` 传入（外层的渠道按钮把 `pinnedProvider === provider` 交给
 * 这里）：高亮画在徽章自身的边框/底色上，不再用外层 `ring` + `ring-offset`。
 * ring 会在徽章外撑出一圈空缝，折行时行高忽大忽小；内联高亮则尺寸恒定。
 */
function ChannelPill({
  provider,
  rate,
  rawId,
  active = false,
}: {
  provider: string;
  rate?: number;
  rawId?: string;
  active?: boolean;
}) {
  const color = providerChartColor(provider);
  const borderColor = `color-mix(in oklch, ${color} ${active ? 65 : 28}%, transparent)`;
  const textColor = `color-mix(in oklch, ${color} ${active ? 70 : 50}%, var(--ink))`;
  return (
    <span
      data-testid={`channel-pill-${provider}`}
      title={rawId}
      className={cn(
        // overflow-hidden 让两段的背景被父级 rounded-full 裁齐；父级只负责外轮廓，
        // 两段各自铺底色，接缝处由右段的 border-l 画分隔线。
        "inline-flex items-stretch overflow-hidden rounded-full border text-[10px] leading-4 font-medium transition-colors",
      )}
      style={{
        // 未选中也留一条很淡的渠道色描边：纯底色在白底上几乎看不见，徽章会散成
        // 一排没有轮廓的文字；选中时描边加粗到能一眼看出「锁的是这个渠道」。
        borderColor,
        fontWeight: active ? 600 : undefined,
      }}
    >
      <span
        className="flex items-center gap-1 py-0 pr-1 pl-1"
        style={{
          backgroundColor: `color-mix(in oklch, ${color} ${active ? 20 : 10}%, transparent)`,
          // 文字往 ink 混：纯渠道色在浅底上对比不足，混太多又失去品牌感。
          color: textColor,
        }}
      >
        <ProviderIcon provider={provider} size={11} />
        {providerLabel(provider)}
      </span>
      {rate !== undefined && (
        // 倍率段：深一档的品牌底 + 左分隔线。数值用 tabular-nums，折行/换渠道
        // 时宽度不抖。
        <span
          className="flex items-center border-l px-1.5 font-bold tabular-nums"
          style={{
            borderColor,
            backgroundColor: `color-mix(in oklch, ${color} ${active ? 32 : 24}%, transparent)`,
            color: `color-mix(in oklch, ${color} ${active ? 80 : 70}%, var(--ink))`,
          }}
        >
          {rateLabel(rate)}
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
        {isCustomAlias(item) && (
          <span data-testid="model-alias-badge" title={aliasTitle(item)}>
            <Badge tone="accent">自定义别名</Badge>
          </span>
        )}
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
  // 可强制指定渠道：多渠道 + 传了 onPin。此时「当前选择」条里的渠道徽章
  // 本身就是按钮，不再另起一行重复列渠道。
  const pinnable = Boolean(onPin) && (selectedItem?.providers.length ?? 0) > 1;
  // 单渠道模型的 value 本身带 @provider（见 modelValue），pinnedProvider 因此
  // 恒非空——但它不是「用户强制指定的」，只是这条模型只有这一个渠道。第三栏
  // 和它的列位只应出现在真的有多渠道可选时。
  const showPin = pinnable && Boolean(pinnedProvider);

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
        /* 三栏：当前选择 | 渠道 | 强制。三栏同一行（窄屏堆叠），中间渠道栏吃剩余
           宽度并在自己栏内折行。第三栏只在真的指定了渠道时渲染 —— 未指定（自动
           路由）时整栏不存在，不留空位、不画空框。 */
        <div
          data-testid="model-current"
          className={cn(
            "grid gap-x-4 gap-y-2 rounded-lg border border-primary/40 bg-primary/[0.06] px-2.5 py-1.5",
            showPin
              ? "sm:grid-cols-[auto_minmax(0,1fr)_auto]"
              : "sm:grid-cols-[auto_minmax(0,1fr)]",
          )}
        >
          {/* 第一栏：当前选择 = 模型名 + 原始 id + 别名/能力分徽章 */}
          <div className="flex min-w-0 flex-wrap items-start gap-x-2 gap-y-1">
            <span className="text-[10px] leading-4 font-medium text-muted-foreground">当前选择</span>
            <span className="flex flex-col">
              <span className="text-sm leading-tight font-semibold">{displayName(selectedItem)}</span>
              {displayName(selectedItem) !== selectedItem.id && (
                <span className="font-mono text-[10px] leading-tight text-muted-foreground">
                  {selectedItem.id}
                </span>
              )}
            </span>
            {isCustomAlias(selectedItem) && (
              <span data-testid="model-current-alias" title={aliasTitle(selectedItem)}>
                <Badge tone="accent">自定义别名</Badge>
              </span>
            )}
            <BenchmarkBadge item={selectedItem} />
          </div>

          {/* 第二栏：渠道。多渠道是「自动路由 + 每个渠道一个按钮」（徽章即按钮，
              点选即强制该渠道）；单渠道 / 未传 onPin 时是同款外观的静态徽章。
              `provider-pin` testid 只挂在可点的那组上——单渠道没有可选渠道，
              测试据此断言「不显示强制指定渠道控件」。 */}
          <div className="flex flex-wrap items-start gap-x-2 gap-y-1">
            <span className="mt-0.5 shrink-0 text-[10px] font-medium text-muted-foreground">
              渠道
            </span>
            <div
              className="flex min-w-0 flex-1 flex-wrap items-center gap-1"
              data-testid={pinnable && onPin ? "provider-pin" : "model-current-channels"}
            >
            {pinnable && onPin ? (
              <>
                <button
                  type="button"
                  data-testid="provider-pin-auto"
                  aria-pressed={pinnedProvider === ""}
                  onClick={() => onPin(baseValue)}
                  className={cn(
                    "mr-0.5 inline-flex items-center gap-1 rounded-full border px-2 py-0 text-[10px] leading-4 font-medium transition-colors focus-visible:ring-2 focus-visible:ring-primary/60 focus-visible:outline-none",
                    pinnedProvider === ""
                      ? "border-primary/50 bg-primary/15 text-primary-ink"
                      : "border-transparent bg-muted/60 text-muted-foreground hover:bg-muted hover:text-foreground",
                  )}
                >
                  <Shuffle className="size-3" />
                  自动路由
                </button>
                {sortProviders(selectedItem.providers).map((provider) => (
                  <button
                    key={provider}
                    type="button"
                    data-testid={`provider-pin-${provider}`}
                    aria-pressed={pinnedProvider === provider}
                    title={`只走 ${providerLabel(provider)}`}
                    onClick={() => onPin(`${baseValue}@${provider}`)}
                    className="rounded-full transition-transform hover:-translate-y-px focus-visible:ring-2 focus-visible:ring-primary/60 focus-visible:outline-none"
                  >
                    <ChannelPill
                      provider={provider}
                      rate={providerRate(selectedItem, provider)}
                      rawId={providerRawId(selectedItem, provider)}
                      active={pinnedProvider === provider}
                    />
                  </button>
                ))}
              </>
            ) : sortProviders(selectedItem.providers).map((provider) => (
              <ChannelPill
                key={provider}
                provider={provider}
                rate={providerRate(selectedItem, provider)}
                rawId={providerRawId(selectedItem, provider)}
              />
            ))}
            </div>
          </div>

          {/* 第三栏：强制。有选择才出现，内容就是「哪个渠道被锁定」。
              这里刻意不用渠道栏的品牌胶囊：那一栏是可点的筛选控件，这里是只读
              状态，同形状会让人以为能点。中性描边 + 品牌图标即可一眼对上号。 */}
          {showPin && (
            <div className="flex items-center gap-1.5" data-testid="model-current-pinned">
              <span className="text-[10px] font-medium text-muted-foreground">强制</span>
              <span className="inline-flex items-center gap-1 rounded-full border border-border bg-muted/50 pr-2 pl-1 py-0 text-[10px] leading-4 font-semibold text-foreground">
                <ProviderIcon provider={pinnedProvider} size={11} />
                {providerLabel(pinnedProvider)}
              </span>
            </div>
          )}
          {!pinnedProvider && !pinnable && selectedItem.providers.length > 1 && (
            <div className="flex items-center gap-1.5">
              <span className="text-[10px] font-medium text-muted-foreground">强制</span>
              <span className="rounded-md bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
                自动路由
              </span>
            </div>
          )}
        </div>
      )}

      <div
        role="listbox"
        data-testid="model-list"
        className="max-h-80 space-y-4 overflow-y-auto rounded-lg border border-border/60 p-2"
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