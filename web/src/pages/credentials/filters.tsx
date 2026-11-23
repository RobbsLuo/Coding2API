import { Search } from "lucide-react";
import { DEFAULT_FILTERS, ANY } from "../../lib/credentialList";
import type { CountedOption, CredentialFilters, GroupSort } from "../../lib/credentialList";
import { Button, Input, Select } from "../../ui";
import { BATCH_BUTTONS, type BatchButton } from "./helpers";

/** 筛选工具条：纯展示层收敛，不改后端排序（后端仍按 sort/order 给全量）。 */
export function CredentialFilterBar({
  filters,
  total,
  providerCounts,
  stateCounts,
  tagCounts,
  groupSort,
  filteredCount,
  filtering,
  onFiltersChange,
  onGroupSortChange,
}: {
  filters: CredentialFilters;
  total: number;
  providerCounts: CountedOption[];
  stateCounts: CountedOption[];
  tagCounts: CountedOption[];
  groupSort: GroupSort;
  filteredCount: number;
  filtering: boolean;
  onFiltersChange: (next: CredentialFilters) => void;
  onGroupSortChange: (next: GroupSort) => void;
}) {
  return (
            <div className="flex flex-wrap items-center gap-2 pb-3" data-testid="credentials-toolbar">
              <div className="relative">
                <Search className="pointer-events-none absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-muted-foreground" />
                <Input
                  value={filters.query}
                  data-testid="credential-search"
                  aria-label="搜索凭证"
                  placeholder="搜索昵称 / 渠道 / id"
                  className="h-8 w-full pl-7 text-xs sm:w-52"
                  onChange={(event) => onFiltersChange({ ...filters, query: event.target.value })}
                />
              </div>
              <Select
                value={filters.provider}
                data-testid="credential-provider-filter"
                aria-label="按渠道筛选"
                className="h-8 w-40 text-xs"
                onChange={(event) => onFiltersChange({ ...filters, provider: event.target.value })}
              >
                <option value={ANY}>全部渠道（{total}）</option>
                {providerCounts.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}（{option.count}）
                  </option>
                ))}
              </Select>
              <Select
                value={filters.state}
                data-testid="credential-state-filter"
                aria-label="按状态筛选"
                className="h-8 w-36 text-xs"
                onChange={(event) => onFiltersChange({ ...filters, state: event.target.value })}
              >
                <option value={ANY}>全部状态（{total}）</option>
                {stateCounts.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}（{option.count}）
                  </option>
                ))}
              </Select>
              {/* 标签筛选：只在池里真的出现过标签时才渲染——没有任何标签时
                  多一个恒为「全部标签」的下拉只是噪声。 */}
              {tagCounts.length > 0 && (
                <Select
                  value={filters.tag}
                  data-testid="credential-tag-filter"
                  aria-label="按标签筛选"
                  className="h-8 w-40 text-xs"
                  onChange={(event) => onFiltersChange({ ...filters, tag: event.target.value })}
                >
                  <option value={ANY}>全部标签（{total}）</option>
                  {tagCounts.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}（{option.count}）
                    </option>
                  ))}
                </Select>
              )}
              {/* 组排序：只重排「渠道组」，不动组内行序（行序仍由列头 / 后端 sort 决定）。
                  默认「额度从高到低」——先看还能打最多的渠道；额度排序时无额度接口的
                  免费层垫底（它们没有「剩余额度」可比）。 */}
              <Select
                value={groupSort}
                data-testid="credential-group-sort"
                aria-label="渠道分组排序"
                className="h-8 w-40 text-xs"
                onChange={(event) => onGroupSortChange(event.target.value as GroupSort)}
              >
                <option value="quota">额度从高到低</option>
                <option value="provider">渠道顺序</option>
              </Select>
              {filtering && (
                <>
                  <span
                    className="text-xs text-muted-foreground"
                    data-testid="credential-filter-count"
                  >
                    筛选后 <strong className="tabular-nums text-foreground">{filteredCount}</strong>
                    {" / "}
                    {total}
                  </span>
                  <Button
                    size="sm"
                    variant="ghost"
                    data-testid="credential-clear-filters"
                    onClick={() => onFiltersChange(DEFAULT_FILTERS)}
                  >
                    清空
                  </Button>
                </>
              )}
            </div>
  );
}

/** 批量操作条：只在有选中项时出现，不占常驻高度。 */
export function BatchActionBar({
  selectedCount,
  busy,
  onRun,
  onClear,
}: {
  selectedCount: number;
  busy: boolean;
  onRun: (button: BatchButton) => void;
  onClear: () => void;
}) {
  return (
              <div
                data-testid="credentials-batch-bar"
                className="mb-3 flex flex-wrap items-center gap-2 rounded-md bg-muted/50 px-3 py-2"
              >
                <span className="text-xs text-muted-foreground" data-testid="batch-selected-count">
                  已选 <strong className="tabular-nums text-foreground">{selectedCount}</strong> 个
                </span>
                {BATCH_BUTTONS.map((button) => (
                  <Button
                    key={button.key}
                    size="sm"
                    variant="default"
                    disabled={busy}
                    data-testid={`batch-${button.key}`}
                    onClick={() => onRun(button)}
                  >
                    {button.verb}
                  </Button>
                ))}
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={busy}
                  data-testid="batch-clear"
                  onClick={onClear}
                >
                  取消选择
                </Button>
              </div>
  );
}
