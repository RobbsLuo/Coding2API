import { Fragment } from "react";
import type { ProviderGroup } from "../../lib/credentialList";
import { rateSummary } from "../../components/ModelPicker";
import type { ProviderModel } from "../../components/ModelPicker";
import { Table, TableBody, TableCell, TableRow } from "../../ui";
import type { Actions, CheckinLogState, CreditEventsState } from "./actions";
import { CreditDrawer, CheckinLogDrawer } from "./drawers";
import { CredentialGroupHeader } from "./groupHeader";
import { supportsGrowth } from "./helpers";
import { ColumnHeaderRow, Row } from "./row";

/**
 * 凭证表格：按渠道分组渲染「组头仪表盘 + 展开后的列头 / 行 / 抽屉」。
 *
 * 展开态、选中集合、删除二次确认与抽屉打开态都由页面壳持有；这里只做
 * 分组内的派生计算（模型清单、额度占比、是否全选），判定口径与原先一致。
 */
export function CredentialTable({
  groups,
  expanded,
  canWrite,
  selected,
  now,
  expiryWindow,
  expirySecondaryWindow,
  tokenWarning,
  busy,
  confirmDelete,
  setConfirmDelete,
  actions,
  creditEvents,
  checkinLog,
  sortCredits,
  toggleSelected,
  toggleGroup,
  toggleGroupSelected,
  modelsByProvider,
  sort,
  order,
  toggle,
}: {
  groups: ProviderGroup[];
  expanded: Set<string>;
  canWrite: boolean;
  selected: Set<string>;
  now: number;
  expiryWindow: number;
  expirySecondaryWindow: number;
  tokenWarning: number;
  busy: boolean;
  confirmDelete: string | null;
  setConfirmDelete: (id: string | null) => void;
  actions: Actions;
  creditEvents: CreditEventsState | null;
  checkinLog: CheckinLogState | null;
  sortCredits: (columnKey: string) => void;
  toggleSelected: (credentialId: string, checked: boolean) => void;
  toggleGroup: (key: string) => void;
  toggleGroupSelected: (ids: string[], checked: boolean) => void;
  modelsByProvider: Map<string, ProviderModel[]>;
  sort: string;
  order: "asc" | "desc";
  toggle: (columnKey: string) => void;
}) {
  return (
    // 长列表限高 + 吸顶列头：列头随每个展开的渠道组渲染（见 ColumnHeaderRow），
    // 滚到第 20 行时列名与操作按钮还在，不必回顶确认「这一列到底是什么」。
    <Table
      data-testid="credentials-table"
      cards
      containerClassName="max-h-[70vh] overflow-auto"
    >
      <TableBody>
        {groups.map((group) => {
          const isCollapsed = !expanded.has(group.key);
          // 成长中心列只在支持的渠道出现（见 supportsGrowth）；**但表格列数
          // 保持恒定**：不支持的渠道由操作列 colSpan 吃掉那一格。表格是 auto
          // 布局，列数一变就重算列宽，展开的下一组会整列错位。
          const showGrowth = supportsGrowth(group.provider);
          // 列数随「是否显示操作列」与「选择列」变化：表格是 auto 布局，
          // 列数一变就重算列宽，展开的下一组会整列错位。选择列只在可写
          // （canWrite）时出现，与 ColumnHeaderRow / Row 的渲染条件严格一致。
          // canWrite：选择 + 凭证 + 状态 + 额度 + token + 成长 + 操作 = 7；
          // viewer：去掉选择与操作两列 = 5。
          const columnCount = canWrite ? 7 : 5;
          // 本组是否已全选：决定组头 checkbox 的勾选态。用 group.items
          // （组内全部行）而不是 filtered，因为组头「全选本渠道」的语义
          // 就是这一渠道下的所有账号。
          const groupIds = group.items.map((item) => item.id);
          const allSelected = groupIds.length > 0
            && groupIds.every((id) => selected.has(id));
          // 本组渠道当前能提供的模型（组头「模型 N 个」+ hover 清单）。
          // 取不到模型列表时为空数组，那块整体不渲染。
          const groupModels = modelsByProvider.get(group.provider) ?? [];
          // 组头顺带给一个「最低倍率」：只看数量看不出这渠道贵不贵，
          // 而最低倍率正是「这渠道值不值得优先用」的答案。免费额度
          // （rate 0）不参与取最低——渠道里混着 1~2 个免费模型时按字面
          // 取最小会显示「最低 免」，读起来像整个渠道免费。
          const rateText = rateSummary(groupModels);
          // 额度合计的进度条：剩余 / 总量（免费层两者皆 null，不出条）
          const quotaPct =
            group.remaining !== null && group.total !== null && group.total > 0
              ? Math.min(100, Math.max(0, (group.remaining / group.total) * 100))
              : null;
          return (
            <Fragment key={group.key}>
              <CredentialGroupHeader
                group={group}
                isCollapsed={isCollapsed}
                canWrite={canWrite}
                allSelected={allSelected}
                groupIds={groupIds}
                groupModels={groupModels}
                rateText={rateText}
                quotaPct={quotaPct}
                expiryWindow={expiryWindow}
                expirySecondaryWindow={expirySecondaryWindow}
                onToggleGroup={toggleGroup}
                onToggleGroupSelected={toggleGroupSelected}
              />
                        {!isCollapsed && (
                          <>
                            <ColumnHeaderRow
                              groupKey={group.key}
                              canWrite={canWrite}
                              showGrowth={showGrowth}
                              sort={sort}
                              order={order}
                              toggle={toggle}
                            />
                            {group.items.map((credential) => (
                              <Fragment key={credential.id}>
                                <Row
                                  credential={credential}
                                  ledger={group.ledger}
                                  now={now}
                                  expiryWindow={expiryWindow}
                                  expirySecondaryWindow={expirySecondaryWindow}
                                  tokenWarning={tokenWarning}
                                  canWrite={canWrite}
                                  showGrowth={showGrowth}
                                  busy={busy}
                                  selected={selected.has(credential.id)}
                                  onSelect={toggleSelected}
                                  confirming={confirmDelete === credential.id}
                                  creditsOpen={creditEvents?.credentialId === credential.id
                                    && creditEvents.ledger === group.ledger}
                                  onConfirmDelete={setConfirmDelete}
                                  actions={actions}
                                />
                                {/* 抽屉由调用方渲染成**独立的下一行**（见 TableBody）。 */}
                                {creditEvents?.credentialId === credential.id
                                  && creditEvents.ledger === group.ledger && (
                                  <TableRow data-testid={`credit-row-${credential.id}`}>
                                    <TableCell colSpan={columnCount} className="p-0">
                                      <CreditDrawer
                                        credentialId={credential.id}
                                        ledger={creditEvents.ledger}
                                        events={creditEvents.events}
                                        sort={creditEvents.sort}
                                        order={creditEvents.order}
                                        onSort={sortCredits}
                                        onClose={() => actions.credits(credential, creditEvents.ledger)}
                                      />
                                    </TableCell>
                                  </TableRow>
                                )}
                                {/* 签到记录抽屉：与流水同为「独立下一行」，一次只开一个。 */}
                                {checkinLog?.credentialId === credential.id && (
                                  <TableRow data-testid={`checkin-row-${credential.id}`}>
                                    <TableCell colSpan={columnCount} className="p-0">
                                      <CheckinLogDrawer
                                        credentialId={credential.id}
                                        events={checkinLog.events}
                                        onClose={() => actions.checkinLog(credential)}
                                      />
                                    </TableCell>
                                  </TableRow>
                                )}
                              </Fragment>
                            ))}
                          </>
                        )}
            </Fragment>
          );
        })}
      </TableBody>
    </Table>
  );
}
