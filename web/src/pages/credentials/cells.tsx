import { TriangleAlert } from "lucide-react";
import {
  activeModelCooldowns,
  formatDuration,
  formatNumber,
  modelCooldownLabel,
} from "../../api/display";
import type { TokenExpiryView } from "../../api/display";
import type { Credential } from "../../api/types";
import { LongTextTip } from "../../components/Tip";
import { Button } from "../../ui";
import { packageExpiry } from "./helpers";

/** 额度包明细：一个账号常有几十个各自独立到期的积分/权益包，
 * 汇总（剩余/总量）看不出"哪些包、什么时候过期"。表格里只在首行
 * 「剩余 / 总量」右侧留一个「套餐 N 个」，鼠标悬浮才弹出完整明细——
 * 几十行不能挤进单元格。无明细（未探测/上游未提供）时不渲染。
 */
export function PackageLadder({ credential }: { credential: Credential }) {
  // 已消耗完（剩余 ≤ 0）的包不再展示：它们只是上游按期返回的历史残留，
  // 既不带可用积分、也不会再被消耗，堆在弹层里只会淹没还有余额的包。
  const packages = (credential.quota_packages ?? []).filter(
    (item) => item.total - item.used > 0,
  );
  const cookieExpired = credential.web_cookie_state === "expired";
  if (packages.length === 0 && !cookieExpired) return null;
  // 展示按到期升序：快过期的排最前（无到期信息的排最后）
  const ordered = [...packages].sort(
    (left, right) => (left.end ?? Infinity) - (right.end ?? Infinity),
  );
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {packages.length > 0 && (
    <LongTextTip
      // 明细行（包名 + 剩余/总量 + 已用 + 到期）比默认 max-w-sm 宽得多，
      // 用窄弹层会每行折成两三行；这里放宽并禁止折行，超出视口时内层横向滚动。
      className="max-w-[90vw] whitespace-nowrap"
      content={
        <div
          data-testid={`packages-${credential.id}`}
          // 高度只能按「弹层实际可用的空间」封顶，不能只写 70vh：弹层贴着视口
          // 上/下缘弹出时（几十个包必然超出），radix 的 flip 挪不动就只能把
          // 上半截顶到视口外。available-height 是 radix 算好的可用高度，
          // 再减掉弹层自身的内边距（py-2 上下共 1rem），否则仍差这几像素。
          className="max-h-[min(70vh,calc(var(--radix-tooltip-content-available-height)-1rem))] max-w-full overflow-x-auto overflow-y-auto"
        >
          <div className="mb-1.5 flex items-baseline justify-between gap-3 border-b border-border pb-1.5">
            <span className="text-xs font-semibold text-foreground">额度包明细</span>
            <span className="rounded-full bg-muted px-1.5 py-0.5 text-[11px] font-medium tabular-nums text-muted-foreground">
              {packages.length} 个 · 按到期先后
            </span>
          </div>
          <ul className="divide-y divide-border/60">
            {ordered.map((item, index) => (
              <li
                key={`${item.name}-${item.end}-${index}`}
                data-testid={`package-${credential.id}`}
                className="py-1 text-xs"
              >
                {item.name ? <span className="font-medium text-foreground">{item.name}：</span> : ""}
                <span className="tabular-nums">剩 {formatNumber(item.total - item.used)} / {formatNumber(item.total)}</span>
                {item.used > 0 && (
                  <span className="text-muted-foreground">（已用 {formatNumber(item.used)}）</span>
                )}
                <span className="text-muted-foreground"> · {packageExpiry(item.end)} 到期</span>
                {item.usable === false && (
                  // 小浣熊的充值池被冻结：额度在，但用不了。与「已用完」分开标
                  <span className="text-destructive" data-testid={`package-frozen-${credential.id}`}>
                    {" "}· 已冻结
                  </span>
                )}
                {(item.expiring ?? 0) > 0 && (
                  // 福利积分按批次到账：同一个「套餐到期日」下仍有更早作废的部分，
                  // 这个数字是上游唯一给出的「最近要过期多少」。
                  <span className="text-destructive" data-testid={`package-expiring-${credential.id}`}>
                    {" "}· 7 天内到期 {formatNumber(item.expiring ?? 0)}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </div>
      }
    >
      <Button
        type="button"
        variant="link"
        className="h-auto cursor-help px-0 text-left text-xs decoration-dotted underline-offset-2"
        data-testid={`packages-toggle-${credential.id}`}
      >
        套餐 {packages.length} 个
      </Button>
    </LongTextTip>
      )}
      {cookieExpired && (
        <span
          className="text-xs text-destructive"
          data-testid={`web-cookie-expired-${credential.id}`}
          title="网页 Cookie 已失效（控制台会话过期）。点「更多 → 网页 Cookie」重新粘贴即可恢复逐笔明细；额度汇总不受影响。"
        >
          网页 Cookie 已失效，请重贴
        </span>
      )}
    </div>
  );
}
/** 生效中的模型级冷却（6004 模型限流 / 11102 该账号无此模型）。
 *
 * 与账号级「冷却中」状态是两回事：整条凭证仍可用，只是这些模型被避开。
 * 表格里只留一个警告图标，具体是哪些模型、还剩多久、连续几次都在 hover 里——
 * 逐条铺开会把额度列撑成好几行（限流时往往同时避让多个模型）。
 */
export function ModelCooldownList({ credential, now }: { credential: Credential; now: number }) {
  const items = activeModelCooldowns(credential, now);
  if (items.length === 0) return null;
  return (
    <LongTextTip
      content={
        <div className="space-y-0.5" data-testid={`model-cooldowns-${credential.id}`}>
          {items.map((item) => (
            <div key={item.model}>
              模型 {item.model}：{modelCooldownLabel(item)}，
              {formatDuration(Math.round(item.cooling_until - now))}后重试
              {item.hits > 1 && `（连续 ${item.hits} 次）`}
            </div>
          ))}
        </div>
      }
    >
      <span
        className="inline-flex cursor-help items-center text-warn"
        data-testid={`model-cooldown-icon-${credential.id}`}
        aria-label={`模型避让 ${items.length} 个`}
      >
        <TriangleAlert className="size-3.5" />
      </span>
    </LongTextTip>
  );
}

/**
 * access token 到期展示。
 *
 * 只说一件事：还剩多久。进度条与「最后续期」都不给：
 * - 进度条的量纲是 token 自己的寿命（`exp - iat`），两个渠道分别是 55 天和
 *   14 天，同一根条在不同渠道间没有可比性；而「还剩几天」本身已经回答了
 *   调度关心的唯一问题。
 * - 「最后续期」需要读者自己拿它与剩余天数做二次推理（刚续期 vs 没人管），
 *   属于解释性信息，不该占表格里的一行。
 *
 * 到期时间未知（后端 0）时显示「—」（与「成长中心」列一致）：渠道没给到期
 * 信息不等于马上过期，但也确实无可展示。
 */
export function TokenExpiry({
  credential,
  view,
}: {
  credential: Credential;
  view: TokenExpiryView;
}) {
  if (view.remaining === null) return <span className="text-muted-foreground">—</span>;
  return (
    <div
      className={view.expiring ? "text-destructive" : "text-muted-foreground"}
      data-testid={`token-expiry-${credential.id}`}
    >
      {view.remaining <= 0 ? "已过期" : view.label}
      {view.expiring && view.remaining > 0 && "，即将到期"}
    </div>
  );
}

/**
 * 额度占比环：SVG 圆环画「剩余 / 总量」，纯图形仪表（不塞圆心文字）。
 *
 * 用环不用条：组头已经是一个多图块的「仪表盘」，横条要占一整行宽度、还只能在
 * 数字右侧挤着；环是定宽正方，跟 28px 的图标块同高，能直接顶掉原来那个图标位。
 *
 * 圆心不放文字：28px 的环内径不到 18px，`100%` 这种四字符放进去必然截断。
 * 精确百分比交给旁边的「额度合计」标签行（11px，可读），环只负责一眼看比例。
 *
 * 用 `stroke-dasharray` 画进度而不是两段 `path`：算出弧长即可，颜色走
 * `currentColor`（父级 `text-ok`），明暗两态自动跟随 token。
 */
export function QuotaRing({ pct }: { pct: number }) {
  // 半径 9、描边 3.5：外径 21.5，在 28px 的方块里留出呼吸，环身也够粗
  const radius = 9;
  const circumference = 2 * Math.PI * radius;
  const remaining = Math.min(circumference, Math.max(0, (pct / 100) * circumference));
  return (
    <span className="grid size-7 shrink-0 place-items-center text-ok">
      <svg viewBox="0 0 24 24" className="size-7 -rotate-90" aria-hidden>
        {/* 空余轨道：整圈淡色，剩余弧叠在上面 */}
        <circle
          cx="12"
          cy="12"
          r={radius}
          fill="none"
          stroke="currentColor"
          strokeOpacity={0.16}
          strokeWidth={3.5}
        />
        {/* 100% 时 dasharray 的零长段配 round 端帽会多出一个圆点，直接画整圈；
            0% 时同理会留下一个点，干脆不画进度弧。 */}
        {remaining >= circumference ? (
          <circle
            cx="12"
            cy="12"
            r={radius}
            fill="none"
            stroke="currentColor"
            strokeWidth={3.5}
          />
        ) : remaining > 0 ? (
          <circle
            cx="12"
            cy="12"
            r={radius}
            fill="none"
            stroke="currentColor"
            strokeWidth={3.5}
            strokeLinecap="round"
            strokeDasharray={`${remaining} ${circumference - remaining}`}
          />
        ) : null}
      </svg>
    </span>
  );
}
