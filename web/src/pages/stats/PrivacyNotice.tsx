/** 隐私与统计口径折叠说明（自 `pages/StatsPage.tsx` 原样搬出，纯静态内容）。 */
import { ChevronDown } from "lucide-react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
  Notice,
} from "../../ui";

export function PrivacyNotice() {
  return (
    <Notice tone="muted" className="items-start px-3 py-2.5 text-xs" data-testid="stats-privacy">
      <Collapsible className="w-full">
        <CollapsibleTrigger className="flex w-full cursor-pointer select-none items-center gap-1.5 text-left transition-colors hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
          <ChevronDown className="size-3.5 shrink-0" />
          <span className="font-medium">隐私与统计口径</span>
          <span className="text-muted-foreground">
            不保存提示词与回答 · 明细保留 90 天 · 成本为刊例价估算
          </span>
        </CollapsibleTrigger>
        <CollapsibleContent className="mt-2 space-y-1.5 leading-relaxed">
          <p>
            隐私：不保存提示词、回答、请求头、Token、工具参数与原始错误体；逐请求明细保留 90 天，小时汇总永久保留；按 API Key 归属用户统计。
          </p>
          <p>
            credit 为渠道可选字段，经常不返回；TRAE 上游不给单请求积分，带 ≈ 的数值是按官方单价折算（CodeBuddy 为上游返回的真值）。健康度只依赖额度探测接口，主指标是 token 数。缓存命中率 = 命中 token ÷ 输入 token，仅在上报过缓存时展示。
          </p>
          <p>
            成本为估算：按 OpenRouter 的模型刊例价（USD / 百万 token）与本服务写入时生效的美元汇率折算，展示人民币、括号内为美元；不是上游真实扣费，未匹配到定价的模型不计入（显示 —）。汇率可在「任务与配置」页热更，仅影响之后写入的请求，历史成本不重算。
          </p>
        </CollapsibleContent>
      </Collapsible>
    </Notice>
  );
}
