import { useCallback, useState } from "react";

export type SortOrder = "asc" | "desc";

/**
 * 列表排序状态（后端排序）：维护当前排序列与方向，点击列头切换。
 *
 * - 点新列：用该列的「首次点击方向」（`firstDirections`，缺省用 `defaultOrder`）。
 *   数值类列通常希望首点降序（先看最大），文本类列首点升序，故按列可配。
 * - 点当前列：升序 ↔ 降序切换。
 *
 * 与 `SortableHead` 搭配：`active={sort === key}`、`direction={order}`。
 */
export function useSort(
  defaultSort: string,
  defaultOrder: SortOrder = "asc",
  firstDirections: Record<string, SortOrder> = {},
) {
  const [state, setState] = useState<{ sort: string; order: SortOrder }>({
    sort: defaultSort,
    order: defaultOrder,
  });

  const toggle = useCallback(
    (columnKey: string) => {
      setState((previous) => {
        if (previous.sort === columnKey) {
          return { sort: columnKey, order: previous.order === "asc" ? "desc" : "asc" };
        }
        return { sort: columnKey, order: firstDirections[columnKey] ?? defaultOrder };
      });
    },
    [defaultOrder, firstDirections],
  );

  return { sort: state.sort, order: state.order, toggle };
}
