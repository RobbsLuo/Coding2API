/**
 * 请求明细的单元格渲染（自 `pages/StatsPage.tsx` 原样搬出）。
 *
 * 分组表与明细表复用同一套单元格：同一行在「历史」与「实时」里显示成同一副
 * 样子（列宽、空值色、合并列规则都不会走偏）。
 */
import { cn } from "cn";
import { TableCell } from "../../ui";
import { Tip } from "../../components/Tip";
import { ProviderIcon } from "../../components/ProviderIcon";
import {
  formatCacheRate,
  formatCompact,
  formatCredit,
  formatLatency,
  formatMoney,
  formatSpeed,
  formatSpeedPair,
  formatTimeParts,
  usageErrorLabel,
} from "../../api/display";
import { providerLabel } from "../../api/providers";
import type { UsageEventRow } from "../../api/types";

/** 凭证文案：昵称优先，回落 ID 前 12 位（ID 很长）。分组表与明细表共用，
 *  保证同一凭证在两处显示成同一个名字。 */
export function credentialCellLabel(name: string | null | undefined, id: string | null | undefined) {
  return name ?? (id ? id.slice(0, 12) : "—");
}

/**
 * 明细时间列：时刻与日期摞成两行（`20:17:09` / `2026-10-21`）。
 *
 * **具体时刻优先**（第一行、正常色），日期降为第二行次要色：看明细时先关心
 * 「几点几分」，日期只在跨天对比时才用得上。两行等宽数字各自纵向对齐，扫读更快，
 * 也避免一行 `toLocaleString` 在窄屏把整行撑宽。
 */
export function timeCell(row: UsageEventRow) {
  const [date, clock] = formatTimeParts(row.ts);
  return (
    <TableCell label="时间" className="whitespace-nowrap tabular-nums leading-tight"
               title={row.request_id ? `请求 ID：${row.request_id}` : undefined}>
      {/* 两行包进同一个块：卡片模式里 td 是 flex 且 space-between，两块子元素会被
          拉到卡片两端（时刻靠左、日期靠右），读起来像两个字段。 */}
      <span className="block">
        <span className="block">{clock}</span>
        <span className="block text-xs text-muted-foreground">{date}</span>
      </span>
    </TableCell>
  );
}

/**
 * 明细「渠道 / 凭证」合并列：凭证名一行（第一行、正常色），渠道 icon + 名称一行（次要色）。
 *
 * **凭证优先**：同渠道下往往挂着多个账号，出问题时先要认出是哪个账号；渠道由图标
 * 一眼可辨，降为第二行次要色即可。两列都是「这条请求打在哪」，拆成两列时凭证列常
 * 只有几个字白占宽度；合并后排序能力靠列头下拉保留（渠道 / 凭证两个子键），
 * 完整凭证 ID 放 title。
 */
export function sourceCell(row: UsageEventRow) {
  return (
    <TableCell label="渠道 / 凭证" className="leading-tight"
               title={row.credential_id ?? undefined}>
      <span className="block min-w-0">
        {/* 固定宽度 + truncate 必须落在内层块元素上：`table-cell` 上的 max-width /
            truncate 不生效，长邮箱（如 hongsheng900805@gmail.com）会把整列撑到
            192px，逼得最后两列横向滚动。窄屏卡片里交给列宽，不再截断。 */}
        <span className="block w-32 truncate max-md:w-auto">
          {credentialCellLabel(row.credential_name, row.credential_id)}
        </span>
        <span className="inline-flex items-center gap-1.5 text-xs whitespace-nowrap text-muted-foreground">
          <ProviderIcon provider={row.provider} size={13} />
          {providerLabel(row.provider)}
        </span>
      </span>
    </TableCell>
  );
}

/**
 * 明细状态列：成功固定文案；失败展示受控错误类型的中文说明（脱敏，不含原始错误体）。
 *
 * 成功是默认态、一屏十行都是它，用绿色文字会满屏噪点，故降为「绿点 + 次要色文字」；
 * 失败反过来用语义色文字 + 红点，让异常行在扫读时先跳出来（色不单独表意，文字始终在）。
 */
export function statusCell(row: UsageEventRow) {
  if (row.ok) {
    return (
      <span className="inline-flex items-center gap-1.5 text-muted-foreground">
        <span aria-hidden className="size-1.5 shrink-0 rounded-full bg-ok" />
        成功
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5 text-danger-ink">
      <span aria-hidden className="size-1.5 shrink-0 rounded-full bg-destructive" />
      {usageErrorLabel(row.error_type)}
    </span>
  );
}

/**
 * 失败行整行标记：左侧 2px 色条 + 极淡语义底。
 *
 * 明细表按时间流排列，失败行散落在成功行之间；只靠状态列一个字上色，扫一屏
 * 十几行时容易漏。整行标记把「这条坏了」变成行级信号，同时保持文字为主信息。
 */
export function rowToneClass(row: UsageEventRow) {
  return row.ok
    ? undefined
    // hover 也要保住色条与底色：基类 TableRow 的 hover:bg-muted/50 会把语义底洗掉，
    // 鼠标停在失败行上时正好失去「这条坏了」的信号。cn 合并时本行覆盖基类。
    : "bg-destructive/[0.04] hover:bg-destructive/10 shadow-[inset_2px_0_0_var(--destructive)]";
}

/**
 * Token 合并列：默认「输入 / 输出 · 命中率」，悬停展开命中 token。
 *
 * 命中率复用 `formatCacheRate`（未上报 / 输入为 0 显示 —）；悬停详情用 `Tip`，
 * 窄屏卡片模式下同样可用（点击单元格即可唤出）。
 */
export function tokenCell(row: UsageEventRow) {
  const rate = formatCacheRate(row.cached_tokens, row.input_tokens);
  return (
    <Tip content={
      `输入 ${formatCompact(row.input_tokens)} · 输出 ${formatCompact(row.output_tokens)}`
      + ` · 命中 ${formatCompact(row.cached_tokens)} · 命中率 ${rate}`
    }>
      <span className="tabular-nums">
        {formatCompact(row.input_tokens)} / {formatCompact(row.output_tokens)}
        <span className="text-muted-foreground"> · {rate}</span>
      </span>
    </Tip>
  );
}

/** 延迟合并列：默认「耗时 / 首字」，悬停补字段名（两个都是 ms/s 的数字，靠提示消歧）。 */
export function latencyCell(row: UsageEventRow) {
  return (
    <Tip content={`耗时 ${formatLatency(row.latency_ms)} · 首字延迟 ${formatLatency(row.ttfb_ms)}`}>
      <span className="tabular-nums">
        {formatLatency(row.latency_ms)}
        <span className="text-muted-foreground"> / {formatLatency(row.ttfb_ms)}</span>
      </span>
    </Tip>
  );
}

/**
 * 数值列的空值弱化：整格都是占位符（—）时降到次要色。
 *
 * 明细表里空值很多（失败的请求没有 token / 成本，Qoder 不上报 credit）。占位符与
 * 真值同色时，一列「—」和一列数字的视觉重量一样，扫读数字反而更费劲。降一档色
 * 让真值浮起来——用 `text-muted-foreground`（不透明度不低于 60%），保证占位符
 * 仍是可读文本而不是装饰。
 */
export function dashTone(empty: boolean) {
  return empty ? "text-muted-foreground" : undefined;
}

/**
 * 明细行单元格（分页表与实时表共用）：两处结构完全一致，抽出来保证同一行
 * 在「历史」与「实时」里显示成同一副样子（列宽、空值色、合并列规则都不会走偏）。
 */
export function eventRowCells(row: UsageEventRow, showUser: boolean) {
  return (
    <>
      {timeCell(row)}
      {showUser && <TableCell label="用户">{row.username}</TableCell>}
      {sourceCell(row)}
      <TableCell label="模型">
        <span className="block max-w-44 truncate" title={row.model}>{row.model}</span>
      </TableCell>
      <TableCell label="状态">{statusCell(row)}</TableCell>
      <TableCell label="Token"
                 className={cn("text-right", dashTone(
                   row.input_tokens == null && row.output_tokens == null))}>
        {tokenCell(row)}
      </TableCell>
      <TableCell label="Credit"
                 className={cn("text-right tabular-nums", dashTone(row.credit == null))}>
        {formatCredit(row.credit, row.credit_estimated)}
      </TableCell>
      <TableCell label="成本"
                 className={cn("text-right tabular-nums", dashTone(row.cost_cny == null))}
                 title={row.cost_usd == null ? undefined : `美元 ${formatMoney(row.cost_usd, "USD")}`}>
        {formatMoney(row.cost_cny, "CNY")}
      </TableCell>
      <TableCell label="延迟"
                 className={cn("text-right", dashTone(
                   row.latency_ms == null && row.ttfb_ms == null))}>
        {latencyCell(row)}
      </TableCell>
      <TableCell label="速度"
                 className={cn("text-right", dashTone(
                   row.tps == null && row.tps_visible == null))}>
        {eventSpeedCell(row)}
      </TableCell>
    </>
  );
}

/** 明细速度单元格（分页明细表与实时表共用，保证两处同口径同文案）。 */
export function eventSpeedCell(row: UsageEventRow) {
  return (
    <Tip content={row.tps === null
      ? "该请求的生成窗口不可测（无首字数据 / 没有输出 token / 生成窗口短于 200ms 或快得不合物理），不计算速度"
      : `正文 ${formatSpeed(row.tps_visible)} · 总产出（含思考）${formatSpeed(row.tps)}`}>
      {/* 单位交给列头（tok/s），行内只留数字：10 列的表里每行重复单位要多占 45px */}
      <span className="tabular-nums">{formatSpeedPair(row.tps_visible, row.tps, false, false)}</span>
    </Tip>
  );
}