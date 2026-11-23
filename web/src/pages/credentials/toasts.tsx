import type { BatchResult, GrowthRunResult } from "../../api/types";
import { batchRowReason, GROWTH_STEP_LABEL, GROWTH_STEP_TONE, growthNotice } from "./helpers";

/**
 * 批量结果浮层：成功数 + 失败明细。
 *
 * 失败原因**逐条列出**而不是只报「失败 3 条」：批量失败几乎总是同一类原因
 * （凭证失效 / 渠道不支持），但混批时可能几种并存，只给计数等于让用户自己
 * 去翻是哪几条。列表上限 5 条，更多的折成「另有 N 条」。
 */
export function BatchToastContent({
  result,
  names,
  done,
}: {
  result: BatchResult;
  names: Map<string, string>;
  /**
   * 结果句的措辞，由**触发按钮**给出而非 `result.action` 反查：
   * 「暂停」与「取消暂停」共用 action=toggle，按 action 查表会把取消暂停的
   * 成功提示写成「已暂停」。
   */
  done: string;
}) {
  const failures = result.results.filter((row) => !row.ok);
  return (
    <div className="space-y-1">
      <div className="font-medium">
        {done}：成功 {result.succeeded} / {result.total}
        {result.failed > 0 && `，失败 ${result.failed}`}
      </div>
      {failures.length > 0 && (
        <ul className="space-y-0.5 text-xs text-muted-foreground">
          {failures.slice(0, 5).map((row) => (
            <li key={row.id} data-testid="batch-failure">
              {names.get(row.id) ?? row.id.slice(0, 12)}：
              {batchRowReason(result.action, row)}
            </li>
          ))}
          {failures.length > 5 && <li>另有 {failures.length - 5} 条失败</li>}
        </ul>
      )}
    </div>
  );
}

/** 成长中心浮层内容：一句话汇报 + 逐步结果。步骤是结构化清单，信息量大，
 * 因此给它比普通提示更长的停留时间，并在浮层内保留完整清单（idle 不是失败，
 * 必须与 failed 分开显示）。 */
export function GrowthToastContent({ result }: { result: GrowthRunResult }) {
  return (
    <div className="space-y-1">
      <div className="font-medium">成长中心：{growthNotice(result)}</div>
      <ul className="space-y-0.5 text-xs text-muted-foreground">
        {result.steps.map((step, index) => (
          <li key={`${step.name}-${index}`} data-testid="growth-step">
            <span className={GROWTH_STEP_TONE[step.status]}>
              {GROWTH_STEP_LABEL[step.status]}
            </span>
            {step.name}
            {step.detail && `：${step.detail}`}
          </li>
        ))}
      </ul>
      {result.session_dead && (
        <div className="text-xs">登录态已失效，需重新登录该渠道后才能领取。</div>
      )}
    </div>
  );
}
