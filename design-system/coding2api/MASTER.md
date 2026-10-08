# Design System — Coding2API 管理台

> **LOGIC:** 构建具体页面前，先看 `design-system/coding2api/pages/[page-name].md`。
> 存在则其规则**覆盖**本文件；否则严格遵循本文件。

---

**Project:** Coding2API
**Updated:** 2026-10-08
**Category:** Developer tool / API gateway control plane
**Source of truth:** `web/src/styles.css`（token） + `web/src/ui/index.tsx`（组件适配层）

> ⚠️ 本文件是**手工核定**的设计系统，不是 `search.py --design-system` 的自动输出。
> 自动输出的配色（#0F172A / #22C55E / Fira）与本项目不符，已弃用。
> 改 token 时**先改 `styles.css`**，再回写本表——不要反向操作。

---

## 1. 设计方向：「Control Plane」工业化控制台

产品故事是**多路渠道汇入单一出口**（gateway）。视觉语言要传达：**实时、可信、可扫读**。

- **保留青绿品牌色**：是产品故事的视觉锚，不做通用蓝/紫。
- **表面分层 > 装饰**：层级靠背景/卡面/浮层的明度梯度表达，不堆阴影或渐变。
- **克制的技术质感**：细刻度、通道色条、等宽数字。**不做** HUD/霓虹/发光。
- **暗色是一等公民**：分层用明度梯度（非阴影），单独校验对比度。

**反模式（禁用）**：emoji 当图标、低对比灰字、布局位移的 hover、`z-[9999]` 乱值、
颜色作唯一状态指示、无 `prefers-reduced-motion` 降级。

---

## 2. 颜色 Token（oklch，语义单源）

### 2.1 品牌语义色

| Token | 亮色 | 暗色 | 用途 |
|---|---|---|---|
| `--primary` | `oklch(0.52 0.13 190)` | `oklch(0.74 0.12 190)` | 品牌主色（青绿） |
| `--primary-foreground` | `oklch(0.99 0 0)` | `oklch(0.17 0.012 258)` | 主色上的文字 |
| `--ok` | `oklch(0.54 0.14 155)` | `oklch(0.70 0.14 155)` | 成功 / 可用 |
| `--warn` | `oklch(0.566 0.15 65)` | `oklch(0.78 0.13 70)` | 警告 / 未探测 / 冷却 |
| `--danger` | `oklch(0.55 0.21 25)` | `oklch(0.70 0.20 22)` | 错误 / 已耗尽 |
| `--ring` | `oklch(0.62 0.11 190)` | `oklch(0.66 0.11 190)` | 焦点环 |

> **对比度约束（亮色）**：语义色若直接当文字用（`text-ok`/`text-warn` 等）必须满足
> 对白底 ≥4.5:1，故亮色 `--ok`/`--warn` 明度比暗色更低一档。放在同色浅底
> （`bg-ok/12` 之类）上的文字，额外混入 25% `--ink`（`text-ok-ink` 等，见
> `styles.css` 的 `--color-*-ink`），因为纯色号在自身浅底上对比会掉到 ~3.4–4.4。

### 2.2 三级表面（关键）

| Token | 亮色 | 暗色 | 用途 |
|---|---|---|---|
| `--surface-sunken` | `oklch(0.968 0.004 252)` | `oklch(0.145 0.012 258)` | 页面底 / 内嵌区 / 代码块 |
| `--surface` | `oklch(0.985 0.003 252)` | `oklch(0.175 0.012 258)` | 页面背景 |
| `--panel` | `oklch(1 0 0)` | `oklch(0.215 0.013 258)` | 卡片 / 面板 |
| `--panel-raised` | `oklch(1 0 0)` | `oklch(0.245 0.014 258)` | 浮层 / 下拉 / 弹窗 |

### 2.3 文字与描边

| Token | 亮色 | 暗色 |
|---|---|---|
| `--ink` | `oklch(0.235 0.016 257)` | `oklch(0.955 0.005 258)` |
| `--ink-muted` | `oklch(0.50 0.016 257)` | `oklch(0.74 0.014 258)` |
| `--border-soft` | `oklch(0.912 0.006 256)` | `oklch(0.295 0.014 258)` |

### 2.4 图表 / 渠道色

渠道色**随渠道稳定**（`web/src/api/providers.ts` 为唯一来源）：`chart-1`..`chart-6`。
新增渠道只改 `providers.ts`，不要在图表里写死颜色。

---

## 3. 字体

| 用途 | Family | 说明 |
|---|---|---|
| 正文 / UI | **Geist Variable**（已内置 `@fontsource-variable/geist`） | 不新增字体依赖 |
| 数字 / ID / 代码 | 系统 mono（`ui-monospace` / SF Mono / Menlo） | 表格数字用 `tabular-nums` |
| 中文回退 | PingFang SC / Microsoft YaHei | `--font-sans` 内置 |

- 基础字号 16px；正文 `leading-relaxed`（1.625）。
- 正文最小 12px（`text-xs`），不得更小。
- 数字列一律 `tabular-nums`（避免翻页时列宽抖动）。

---

## 4. 间距 / 圆角 / 阴影 / 动效

### 间距（4/8 基准）
`0.25rem`(1) · `0.5rem`(2) · `0.75rem`(3) · `1rem`(4) · `1.5rem`(6) · `2rem`(8) · `3rem`(12)
- 卡片内边距 `1rem`(默认) / `0.75rem`(sm)。
- 区块垂直间距 `1.5rem`（`space-y-6`）。

### 圆角
`--radius: 0.75rem`（12px）；`sm/md/lg/xl/2xl` 由倍数派生。

### 阴影（亮色靠阴影，暗色靠明度）
| Token | 亮色 | 暗色 |
|---|---|---|
| `--shadow-sm` | `0 1px 2px oklch(0 0 0/0.05)` | 微弱 |
| `--shadow-md` | `0 2px 8px -2px oklch(0 0 0/0.08)` | 更强 |
| `--shadow-lg` | `0 12px 32px -12px oklch(0 0 0/0.18)` | 更强 |

### 动效
`--motion-fast: 120ms` · `--motion-base: 180ms`，缓动 `ease`。
**必须**在 `@media (prefers-reduced-motion: reduce)` 下关闭非必要过渡/动画。

---

## 5. 组件规范

组件统一从 `web/src/ui/index.tsx` 取（它是 shadcn 适配层）。**不要**在页面里直接拼裸样式。

| 组件 | 规范 |
|---|---|
| `Panel` | 卡片容器；`rounded-xl` + `ring-1 ring-border` + `shadow-sm`；标题行 + `action` 槽 |
| `Button` | 变体 `default`(描边)/`primary`/`danger`/`ghost`/`link`；焦点环可见 |
| `Badge` | 语义色调 `ok/warn/danger/muted/accent`；**必须带文字**，色不单独表意 |
| `Metric` | KPI 卡：标签 + 大号 `tabular-nums` 数值 + hint；可选 icon 与 tone |
| `Notice` | 行内提示；`danger` 用 `role=alert`；左侧语义色条 |
| `ToastViewport` + `useToasts` | 操作结果浮层（顶部居中，`web/src/components/Toast.tsx`）：数秒自动消失、可手动关闭；`danger` 用 `role=alert`、其余 `role=status`；同 `testId` 只保留最新一条；语气用 `Badge`/`Notice` 同款 tint 底 + `-ink` 图标（亮色口径色描边叠白底对比过低，不可只用描边） |
| `EmptyState` | 空态：图标 + 标题 + 描述 + 可选操作 |
| `Tabs` | 分段器；激活项 `bg-background` + `shadow-sm` |
| `Table` | 圆角容器 + 表头底色 + 行 hover；移动端横向滚动 |
| `PageHeader` | eyebrow（可选）+ 标题 + 说明 + 右侧操作槽 |
| `PageSkeleton` | 加载骨架，替代纯文字「载入中…」 |

---

## 6. 无障碍（硬约束）

- [ ] 正文对比度 ≥ 4.5:1；非文本 UI（边框/图标）≥ 3:1
- [ ] 焦点可见（`focus-visible` 环），不隐藏 outline
- [ ] 状态/语义不只靠颜色（徽章带文字）
- [ ] `prefers-reduced-motion` 生效
- [ ] 表单字段有 label；错误贴近字段并 `role=alert`
- [ ] 图标按钮有 `aria-label`；装饰图标 `aria-hidden`
- [ ] 对话框焦点管理与 Esc 关闭

---

## 7. 交付前检查

- [ ] `pnpm exec tsc --noEmit` / `pnpm exec vitest run` / `pnpm build` 全绿
- [ ] 明暗两态 + 375/768/1024/1440 走查
- [ ] 键盘 Tab 顺序符合视觉顺序
- [ ] 无横向滚动
- [ ] 内容不被固定导航遮挡
