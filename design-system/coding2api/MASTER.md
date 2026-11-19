# Design System — Coding2API 管理台

> 结论：全仓唯一设计规范，「Control Plane」工业化控制台——青绿品牌色、三级表面分层、克制的技术质感、暗色是一等公民。建页一律遵循本文件。

**项目**：Coding2API ｜ **更新**：2026-10-30 ｜ **真源**：token 在 `web/src/styles.css`，组件适配层在 `web/src/ui/index.tsx`（原语 `web/src/components/ui/`）。

改 token **先改 `styles.css`** 再回写本表，不要反向操作。

## 1. 视觉方向

产品故事：**多路渠道汇入单一出口**（gateway）。语言：**实时、可信、可扫读**。

- 保留青绿品牌色（视觉锚，不做通用蓝 / 紫）；**表面分层 > 装饰**：层级靠背景 / 卡面 / 浮层明度梯度，不堆阴影或渐变。
- 克制技术质感：细刻度、通道色条、等宽数字。**不做** HUD / 霓虹 / 发光。
- 暗色一等公民：分层用明度梯度（非阴影），单独校验对比度。

**反模式（禁用）**：emoji 当图标、低对比灰字、布局位移的 hover、`z-[9999]` 乱值、颜色作唯一状态指示、无 `prefers-reduced-motion` 降级。

## 2. Token（oklch，语义单源）

品牌语义色（亮 / 暗）：`--primary` `0.52 0.13 190` / `0.74 0.12 190`（青绿）｜`--primary-foreground` `0.99 0 0` / `0.17 0.012 258`｜`--ok` `0.54 0.14 155` / `0.70 0.14 155`（成功）｜`--warn` `0.566 0.15 65` / `0.78 0.13 70`（警告 / 未探测 / 冷却）｜`--danger` `0.55 0.21 25` / `0.70 0.20 22`（错误 / 耗尽）｜`--ring` `0.62 0.11 190` / `0.66 0.11 190`。

三级表面（亮 / 暗）：`--surface-sunken` `0.968 0.004 252` / `0.145 0.012 258`（页面底 / 内嵌 / 代码块）｜`--surface` `0.985 0.003 252` / `0.175 0.012 258`（页面背景）｜`--panel` `1 0 0` / `0.215 0.013 258`（卡片）｜`--panel-raised` `1 0 0` / `0.245 0.014 258`（浮层 / 下拉 / 弹窗）。

文字与描边（亮 / 暗）：`--ink` `0.235 0.016 257` / `0.955 0.005 258`｜`--ink-muted` `0.50 0.016 257` / `0.74 0.014 258`｜`--border-soft` `0.912 0.006 256` / `0.295 0.014 258`。

- **对比度约束（亮色）**：语义色作文字对白底 ≥4.5:1，故 `--ok` / `--warn` 明度比暗色低一档；同色浅底（`bg-ok/12`）上的文字混入 25% `--ink`（`text-ok-ink` 等，见 `styles.css` 的 `--color-*-ink`）——纯色号在自身浅底只有 ~3.4–4.4。
- **图表 / 渠道色**：`chart-1`..`chart-6`，随渠道稳定，唯一来源 `web/src/api/providers.ts`；新增渠道只改该文件。

## 3. 字体与尺寸

| 用途 | Family |
|---|---|
| 正文 / UI | **Geist Variable**（已内置 `@fontsource-variable/geist`，不新增字体依赖） |
| 数字 / ID / 代码 | 系统 mono（`ui-monospace` / SF Mono / Menlo），一律 `tabular-nums` |
| 中文回退 | PingFang SC / Microsoft YaHei（`--font-sans` 内置） |

- 基础字号 16px，正文 `leading-relaxed`（1.625），最小 12px（`text-xs`）。
- 间距 4/8 基准：`0.25rem` / `0.5` / `0.75` / `1` / `1.5` / `2` / `3`；卡片内边距 `1rem`（`0.75rem` sm），区块垂直 `1.5rem`（`space-y-6`）。
- 圆角 `--radius: 0.75rem`（12px），`sm/md/lg/xl/2xl` 由倍数派生。
- 阴影：亮色靠阴影（`--shadow-sm` `0 1px 2px oklch(0 0 0/0.05)`、`md` `0 2px 8px -2px /0.08`、`lg` `0 12px 32px -12px /0.18`），暗色靠明度。
- 动效 `--motion-fast: 120ms` / `--motion-base: 180ms`，缓动 `ease`；**必须**在 `prefers-reduced-motion: reduce` 下关闭非必要过渡。

## 4. 组件

统一取 `web/src/ui/index.tsx`，页面**不要**直接拼裸样式。

| 组件 | 规范 |
|---|---|
| `Panel` | `rounded-xl` + `ring-1 ring-border` + `shadow-sm`；标题行 + `action` 槽 |
| `Button` | `default`(描边) / `primary` / `danger` / `ghost` / `link`；焦点环可见 |
| `Badge` / `NavBadge` | 色调 `ok/warn/danger/muted/accent`，**必须带文字**；`NavBadge` 用 `tabular-nums`、`>99` → `99+`、0 不渲染，数字 `aria-hidden` + `sr-only` 全语义文案 |
| `Metric` / `EmptyState` / `PageSkeleton` | KPI 卡（标签 + 大号 `tabular-nums` + hint）；空态（图标 + 标题 + 描述 + 操作）；加载骨架替代「载入中…」 |
| `Notice` / `ToastViewport` | 行内提示 / 操作结果浮层；`danger` 用 `role=alert`、其余 `role=status`；tint 底 + `-ink` 图标（亮色口径只用描边对比过低）；同 `testId` 只留最新 |
| `Tabs` / `Table` | 分段器（激活 `bg-background` + `shadow-sm`）；表格圆角容器 + 表头底色 + 行 hover，`<768px` 用 `cards` 转卡片（每格 `TableCell label` 显示字段名，DOM 不变） |
| `PageHeader` | eyebrow（可选）+ 标题 + 说明 + 右侧操作槽 |
| `Tip` / `LongTextTip` | 短提示走 `ink` 档（反色气泡）；多行清单走 `panel` 档（`panel-raised` + `ring-border` + `shadow-lg`，**不出箭头**），左对齐 `items-start` |

## 5. 无障碍（硬约束）

- [ ] 正文对比度 ≥ 4.5:1；非文本 UI（边框 / 图标）≥ 3:1
- [ ] 焦点可见（`focus-visible` 环），不隐藏 outline
- [ ] 状态 / 语义不只靠颜色（徽章带文字）
- [ ] `prefers-reduced-motion` 生效
- [ ] 表单字段有 label；错误贴近字段并 `role=alert`
- [ ] 图标按钮有 `aria-label`；装饰图标 `aria-hidden`
- [ ] 对话框焦点管理与 Esc 关闭

## 6. 交付前检查

- [ ] `pnpm exec tsc --noEmit` / `pnpm exec vitest run` / `pnpm build` 全绿
- [ ] 明暗两态 + 375 / 768 / 1024 / 1440 走查
- [ ] 键盘 Tab 顺序符合视觉顺序；无横向滚动；内容不被固定导航遮挡
