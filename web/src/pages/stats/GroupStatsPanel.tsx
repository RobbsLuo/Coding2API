/**
 * 分组统计：四维度表格 + 分位档切换 + 口径说明（自 `pages/StatsPage.tsx` 原样搬出）。
 *
 * 分组键列名、行数据、排序状态都由页面传入：维度切换会换一个查询，
 * 取数仍在页面（各维度 hook 的 enabled 依赖同一份判断）。
 */
import { Empty, Footnote, Panel, Table, TableBody, TableCell, TableHeader,
  TableRow, Tabs } from "../../ui";
import { Tip } from "../../components/Tip";
import { ProviderIcon } from "../../components/ProviderIcon";
import { SortableHead } from "../../components/SortableHead";
import { SortMenuHead } from "../../components/SortMenuHead";
import {
  formatCacheRate,
  formatCompact,
  formatCredit,
  formatLatency,
  formatMoney,
  formatNumber,
  formatSpeed,
  formatSpeedPair,
} from "../../api/display";
import { providerLabel } from "../../api/providers";
import type { StatsDrill } from "../../api/types";
import {
  GROUP_AVG_LATENCY_COLUMNS,
  GROUP_REQUEST_COLUMNS,
  GROUP_TOKEN_COLUMNS,
  PERCENTILE_TABS,
  SPEED_COLUMNS,
  type GroupDimension,
  type GroupRow,
  type Percentile,
  type StatsSortState,
} from "./constants";
import { credentialCellLabel } from "./cells";

interface GroupStatsPanelProps {
  groupDimension: string;
  groupDimensions: readonly GroupDimension[];
  onGroupDimensionChange: (value: string) => void;
  percentile: Percentile;
  onPercentileChange: (value: string) => void;
  groupSort: StatsSortState;
  group: GroupDimension;
  groupRows: GroupRow[];
  onDrill: (next: StatsDrill) => void;
}

export function GroupStatsPanel(props: GroupStatsPanelProps) {
  const {
    groupDimension, groupDimensions, onGroupDimensionChange,
    percentile, onPercentileChange, groupSort, group, groupRows, onDrill,
  } = props;
  const groupKey = group.groupKey;
  const groupLabel = group.label2;
  return (
    <Panel title="分组统计" action={
      <Tabs
        value={groupDimension}
        options={groupDimensions.map(({ value, label }) => ({ value, label }))}
        onChange={onGroupDimensionChange}
        testId="group-tabs"
      />
    }>
      {/* 分位档切换：四档后端一次算好，这里只决定展示哪一档（避免四列撑爆表头） */}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <span className="text-xs text-muted-foreground">延迟分位</span>
        <Tabs value={percentile} options={PERCENTILE_TABS}
              onChange={(value) => onPercentileChange(value as Percentile)}
              testId="percentile-tabs" compact />
        <span className="text-xs text-muted-foreground">
          取自逐请求明细（仅 90 天），只统计成功请求
        </span>
      </div>
      {groupRows.length === 0 ? (
        <Empty data-testid="no-group-stats">该范围内没有请求</Empty>
      ) : (
        <Table data-testid="group-table" cards>
          <TableHeader>
            <TableRow>
              <SortableHead label={group.value === "provider" ? "渠道" : groupLabel}
                            columnKey="group" active={groupSort.sort === "group"}
                            direction={groupSort.order} onToggle={groupSort.toggle}
                            testId="sort-group" />
              <SortMenuHead label="请求" options={[...GROUP_REQUEST_COLUMNS]}
                            activeKey={groupSort.sort} direction={groupSort.order}
                            onToggle={groupSort.toggle} testId="group-sort-requests" />
              <SortableHead label="Credit" columnKey="credit"
                            active={groupSort.sort === "credit"} direction={groupSort.order}
                            onToggle={groupSort.toggle} align="right" testId="sort-credit" />
              <SortableHead label="成本" columnKey="cost_cny"
                            active={groupSort.sort === "cost_cny"} direction={groupSort.order}
                            onToggle={groupSort.toggle} align="right" testId="sort-cost_cny" />
              {/* Token 合并列：输入 / 输出 · 命中率，悬停展开命中 token。
                  列头下拉仍可分别按输入 / 输出 / 命中排序。 */}
              <SortMenuHead label="Token" options={[...GROUP_TOKEN_COLUMNS]}
                            activeKey={groupSort.sort} direction={groupSort.order}
                            onToggle={groupSort.toggle} testId="group-sort-token" />
              {/* 速度合并列：正文 / 总产出（含思考）——同分母（生成时间总和，
                  读明细、仅 90 天），分子不同：正文贴近屏幕上看到的刷新速度，
                  总产出含思考 token（思考成批快速吐出，数字会明显更高）。无首字
                  数据时回落估算并标 ≈。列头下拉可分别按两个分子排序。 */}
              <SortMenuHead label="速度" options={[...SPEED_COLUMNS]}
                            activeKey={groupSort.sort} direction={groupSort.order}
                            onToggle={groupSort.toggle} testId="group-sort-speed" />
              {/* 延迟两列：均值读小时汇总（与总览同口径），分位读明细表（仅 90 天）。
                  两套口径各自合并成一列，靠表尾说明与悬停文案区分。 */}
              <SortMenuHead label="平均延迟" options={[...GROUP_AVG_LATENCY_COLUMNS]}
                            activeKey={groupSort.sort} direction={groupSort.order}
                            onToggle={groupSort.toggle} testId="group-sort-avg-latency" />
              <SortMenuHead label={`${percentile.toUpperCase()} 延迟`}
                            options={[
                              { key: `${percentile}_latency_ms`,
                                label: `${percentile.toUpperCase()} 耗时` },
                              { key: `${percentile}_ttfb_ms`,
                                label: `${percentile.toUpperCase()} 首字` },
                            ]}
                            activeKey={groupSort.sort} direction={groupSort.order}
                            onToggle={groupSort.toggle}
                            testId={`group-sort-${percentile}-latency`} />
            </TableRow>
          </TableHeader>
          <TableBody>
            {groupRows.map((row) => {
              // 点行 = 钻取到该维度的明细；分组键 → 明细过滤字段的映射：
              // provider/model/credential_id 直接同名，用户维度走作用域 username
              const keyValue = String(row[groupKey]);
              const next: StatsDrill = group.value === "user"
                ? { username: keyValue }
                : group.value === "provider" ? { provider: keyValue }
                  : group.value === "model" ? { model: keyValue }
                    : { credential_id: keyValue };
              return (
              <TableRow key={keyValue}
                        data-testid={`group-row-${keyValue}`}
                        className="cursor-pointer"
                        tabIndex={0}
                        role="button"
                        aria-label={`查看 ${keyValue} 的请求明细`}
                        onClick={() => onDrill(next)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter" || event.key === " ") {
                            event.preventDefault();
                            onDrill(next);
                          }
                        }}>
                <TableCell label={group.value === "provider" ? "渠道" : groupLabel}
                           className="max-w-48 truncate"
                           title={groupKey === "credential_id"
                             ? (row.credential_id ?? undefined)
                             : keyValue}>
                  {groupKey === "provider" ? (
                    <span className="inline-flex items-center gap-1.5">
                      <ProviderIcon provider={String(row.provider)} size={13} />
                      {providerLabel(String(row.provider))}
                    </span>
                  ) : groupKey === "credential_id" ? (
                    // 渠道 icon + 凭证名：与请求明细的凭证列同口径
                    <span className="inline-flex items-center gap-1.5">
                      <ProviderIcon provider={String(row.provider)} size={13} />
                      {credentialCellLabel(row.credential_name, row.credential_id)}
                    </span>
                  ) : (
                    keyValue
                  )}
                </TableCell>
                {/* 请求合并列：默认「请求数 · 成功率」，悬停补成功数。
                    成功率 = 成功 ÷ 请求，请求数为 0 时给 —（不渲染 0% 冒充数据）。 */}
                <TableCell label="请求" className="text-right">
                  <Tip content={`请求 ${formatNumber(row.requests)}`
                    + ` · 成功 ${formatNumber(row.ok_count)}`
                    + ` · 成功率 ${row.requests > 0
                      ? `${((row.ok_count / row.requests) * 100).toFixed(1)}%`
                      : "—"}`}>
                    <span className="tabular-nums">
                      {formatNumber(row.requests)}
                      <span className="text-muted-foreground" data-testid="group-success-rate">
                        {" · "}{row.requests > 0
                          ? `${((row.ok_count / row.requests) * 100).toFixed(1)}%`
                          : "—"}
                      </span>
                    </span>
                  </Tip>
                </TableCell>
                <TableCell label="Credit" className="text-right tabular-nums">
                  {formatCredit(row.credit, row.credit_estimated)}
                </TableCell>
                <TableCell label="成本" className="text-right tabular-nums"
                           title={`美元 ${formatMoney(row.cost_usd, "USD")}`}>
                  {formatMoney(row.cost_cny, "CNY")}
                </TableCell>
                <TableCell label="Token" className="text-right"
                           data-testid="group-token">
                  <Tip content={
                    `输入 ${formatCompact(row.input_tokens)}`
                    + ` · 输出 ${formatCompact(row.output_tokens)}`
                    + ` · 命中 ${formatCompact(row.cached_tokens)}`
                  }>
                    <span className="tabular-nums">
                      {formatCompact(row.input_tokens)} / {formatCompact(row.output_tokens)}
                      <span className="text-muted-foreground" data-testid="group-cache-rate">
                        {" · "}{formatCacheRate(row.cached_tokens, row.input_tokens)}
                      </span>
                    </span>
                  </Tip>
                </TableCell>
                {/* 速度合并列：正文 / 总产出；无合格样本整格 —（估算口径前置 ≈）。
                    正文为 0（整段输出都是思考）时正文侧显示 —，不拿 0 冒充速度。 */}
                <TableCell label="速度" className="text-right tabular-nums"
                           data-testid="group-tps">
                  <Tip content={row.tps === null && row.tps_visible === null
                    ? "该组没有产出了 token 的成功请求，或生成窗口不可测，不计算速度"
                    : `正文 ${formatSpeed(row.tps_visible, row.tps_estimated)}`
                      + ` · 总产出（含思考）${formatSpeed(row.tps, row.tps_estimated)}`}>
                    <span>{formatSpeedPair(row.tps_visible, row.tps, row.tps_estimated)}</span>
                  </Tip>
                </TableCell>
                {/* 延迟两列：均值与分位都只算成功请求；无成功请求显示 — */}
                <TableCell label="平均延迟" className="text-right"
                           data-testid="group-avg-latency">
                  <Tip content={`平均耗时 ${formatLatency(row.avg_latency_ms)}`
                    + ` · 平均首字 ${formatLatency(row.avg_ttfb_ms)}`}>
                    <span className="tabular-nums">
                      {formatLatency(row.avg_latency_ms)}
                      <span className="text-muted-foreground" data-testid="group-avg-ttfb">
                        {" / "}{formatLatency(row.avg_ttfb_ms)}
                      </span>
                    </span>
                  </Tip>
                </TableCell>
                <TableCell label={`${percentile.toUpperCase()} 延迟`} className="text-right"
                           data-testid={`group-${percentile}-latency`}>
                  <Tip content={`${percentile.toUpperCase()} 耗时 `
                    + `${formatLatency(row[`${percentile}_latency_ms`])}`
                    + ` · ${percentile.toUpperCase()} 首字 `
                    + `${formatLatency(row[`${percentile}_ttfb_ms`])}`}>
                    <span className="tabular-nums">
                      {formatLatency(row[`${percentile}_latency_ms`])}
                      <span className="text-muted-foreground"
                            data-testid={`group-${percentile}-ttfb`}>
                        {" / "}{formatLatency(row[`${percentile}_ttfb_ms`])}
                      </span>
                    </span>
                  </Tip>
                </TableCell>
              </TableRow>
              );
            })}
          </TableBody>
        </Table>
      )}
      <Footnote summary="口径说明（数据源 · 延迟 · 速度）" testId="group-footnote">
        <p>
          {groupKey === "credential_id"
            ? "凭证分组取自逐请求明细（仅保留 90 天），无凭证的预热失败等请求不计入。"
            : "各维度数据源为小时汇总（永久保留），最近 5 分钟内的请求可能尚未计入。"}
          {" "}点任意一行可钻取到下方「请求明细」。
        </p>
        {/* 延迟列的两套口径必须写明：均值永久、分位只覆盖 90 天，
            混在同一行里不说明会被读成「同一个范围」。 */}
        <p>
          延迟列只统计成功请求。平均耗时 / 平均首字取自小时汇总（与总览卡片同口径）；
          分位为第 P50/P90/P95/P99 百分位（向上取整名次，默认 P95），取自逐请求明细，
          只覆盖最近 90 天。速度列两个分子分母相同（生成时间总和，不是逐条平均），正文
          token（output − reasoning）贴近屏幕上看到的刷新速度，总产出 token（含思考）
          含思考成批吐出的部分；成功请求只要有产出就计入（不设输出量下限），并剔除生成
          窗口 &lt;200ms 或单条 &gt;1000 tok/s 的样本——那是上游把内容攒成一批吐出、生成
          时长根本不可测，不是「很快」。无首字数据时按端到端耗时估算（同样不设输出门槛，
          分母含首字等待，前置 ≈），同样取自逐请求明细（仅 90 天）。
        </p>
      </Footnote>
    </Panel>
  );
}
