import { useState } from "react";
import { Button, Panel } from "../ui";

interface HelpEntry {
  term: string;
  where: string;
  meaning: string;
}

const HELP: { title: string; entries: HelpEntry[] }[] = [
  {
    title: "凭证状态怎么看",
    entries: [
      {
        term: "可用",
        where: "凭证管理 · 状态列",
        meaning: "该凭证当前能被调度器选中处理请求。",
      },
      {
        term: "冷却中（显示剩余时间）",
        where: "凭证管理 · 状态列",
        meaning:
          "上游暂时拒绝（权益耗尽 12 小时、限流 60 秒、连续出错 10 分钟），到期自动恢复，无需手动操作。",
      },
      {
        term: "已禁用",
        where: "凭证管理 · 状态列",
        meaning: "上游判定会话失效，凭证已永久停止使用；删除后重新登录该账号即可。",
      },
      {
        term: "已关闭",
        where: "凭证管理 · 状态列",
        meaning: "管理员手动停用（软开关），随时可以重新启用。",
      },
      {
        term: "健康度：百分比",
        where: "凭证管理 · 健康度列",
        meaning: "剩余积分占总积分的比例，调度器优先选数值高的。",
      },
      {
        term: "健康度：未探测到额度",
        where: "凭证管理 · 健康度列",
        meaning:
          "探测失败或上游没返回额度信息。注意它不是「已耗尽」——点「探测」可重新获取。",
      },
      {
        term: "额度下方的时间语义",
        where: "凭证管理 · 额度列",
        meaning:
          "CodeBuddy 是「本周期剩余，<日期> 重置」；TRAE 是「账户剩余（单调递减）」。两者单位都是积分，但重置行为不同。",
      },
    ],
  },
  {
    title: "每个按钮做什么",
    entries: [
      {
        term: "探测",
        where: "凭证管理 · 操作列",
        meaning: "立即向上游查询一次剩余额度。新增凭证后系统已自动探测一次，失败或想刷新时手动点。",
      },
      {
        term: "签到",
        where: "凭证管理 · 操作列",
        meaning: "领取当日积分。每天 9 点系统自动为所有可用凭证签到（可在配置改时间），这里手动触发。",
      },
      {
        term: "账号",
        where: "凭证管理 · 操作列",
        meaning: "同一登录下的个人/企业账号切换（仅 CodeBuddy 支持）。切换后额度会立即重新探测。",
      },
      {
        term: "指定 / 取消指定",
        where: "凭证管理 · 操作列",
        meaning: "把该凭证设为优先使用的唯一凭证（全局只能指定一个）。排查问题或想固定用某个号时用。",
      },
      {
        term: "停用 / 启用",
        where: "凭证管理 · 操作列",
        meaning: "临时把凭证移出调度池，不删除数据。",
      },
      {
        term: "登录 CodeBuddy / 登录 TRAE",
        where: "凭证管理 · 登录上游账号",
        meaning:
          "CodeBuddy 走设备码授权（本页自动轮询结果）；TRAE 在浏览器完成授权后由本服务直接接收回调。远程部署时需保证 PUBLIC_BASE_URL 是浏览器可达的地址。",
      },
    ],
  },
  {
    title: "调用相关的隐含规则",
    entries: [
      {
        term: "模型名 model@provider",
        where: "Playground · 强制指定上游",
        meaning:
          "默认 glm-5.2 由调度器在两个上游间自动选健康的；写 glm-5.2@trae 则只走 TRAE，glm-5.2@codebuddy 只走 CodeBuddy。",
      },
      {
        term: "统计里的 credit 是 —",
        where: "用量统计",
        meaning:
          "credit 是上游可选字段，经常不返回；健康度只依赖额度探测接口。主指标是 token 数。",
      },
      {
        term: "请求记在谁头上",
        where: "用量统计 · 用户名筛选",
        meaning:
          "按 API Key 的归属用户统计。管理员可以查任意用户；普通用户只能看自己。",
      },
      {
        term: "接入第三方客户端",
        where: "任意 OpenAI 兼容客户端",
        meaning:
          "Base URL 填本服务地址加 /v1，API Key 用「API Key」页创建的 sk-…，模型名从 /v1/models 里选。",
      },
    ],
  },
];

export function HelpPanel() {
  const [open, setOpen] = useState(false);

  if (!open) {
    return (
      <Button
        size="sm"
        variant="ghost"
        data-testid="help-toggle"
        onClick={() => setOpen(true)}
      >
        功能说明
      </Button>
    );
  }

  return (
    <Panel
      title="功能说明"
      action={
        <Button size="sm" variant="ghost" data-testid="help-close" onClick={() => setOpen(false)}>
          收起
        </Button>
      }
    >
      <div className="space-y-5 text-sm" data-testid="help-content">
        {HELP.map((section) => (
          <section key={section.title}>
            <h3 className="mb-2 text-xs font-semibold tracking-wide text-[var(--color-accent)]">
              {section.title}
            </h3>
            <dl className="space-y-2.5">
              {section.entries.map((entry) => (
                <div key={entry.term} className="rounded-lg border border-[var(--color-border-soft)] px-3 py-2">
                  <dt className="font-medium">{entry.term}</dt>
                  <dd className="mt-0.5 text-xs text-[var(--color-ink-muted)]">
                    位置：{entry.where}
                    <div className="mt-1 text-[var(--color-ink)]">{entry.meaning}</div>
                  </dd>
                </div>
              ))}
            </dl>
          </section>
        ))}
      </div>
    </Panel>
  );
}
