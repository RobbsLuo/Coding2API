# 同类开源项目横向对照与候选功能清单（COD-7）

调研日期：2026-11-12。仅调研与方案，不改生产代码、不加依赖。
参照项目版本取各仓库 `releases/latest` 与最近 push 时间。

## 1. 参照项目

| 项目 | 定位 | 版本 / 最近更新 | 来源 |
|---|---|---|---|
| LiteLLM | 100+ 供应商统一网关 + Python SDK，虚拟 Key、花费追踪、护栏、负载均衡、管理台 | v1.109.0（2026-11-11），push 2026-11-11 | https://github.com/BerriAI/litellm |
| One API | 多供应商统一 API 与 Key 二次分发，令牌额度/过期、渠道分组、兑换码、失败重试、单二进制 | v0.6.10（2025-02-02），push 2026-01-09 | https://github.com/songquanpeng/one-api |
| New API | 自托管 AI 网关，OpenAI/Claude/Gemini 三协议、配额与订阅、缓存计费、表达式定价、Redis 多节点、7 语言管理台 | v1.0.0-rc.49（2026-11-08），push 2026-11-11 | https://github.com/Calcium-Ion/new-api |
| Portkey Gateway | 高性能网关，重试/回退、负载均衡、条件路由、护栏、MCP Gateway | v1.15.2（2026-01-12），push 2026-05-25 | https://github.com/portkey-ai/gateway |
| Claude Code Router | 面向编码 Agent 的本地控制面，多 Agent 统一入口、供应商/模型路由、凭据池轮换、有序回退、工具增强 | v3.1.3（2026-10-09），push 2026-10-10 | https://github.com/musistudio/claude-code-router |

## 2. 逐项对照（现状 → 差距）

Coding2API 已有：OpenAI Chat/Responses + Anthropic Messages、十路逆向渠道、公共凭证池与健康调度、凭证权重、跨渠道 fallback 组、Key 级日配额（请求/token/成本 + RPM/TPM/并发）、到期额度优先、三角色与审计、脱敏统计/账单导出、出口 IP 粘性、上游状态与告警、热更配置、Playground、备份恢复。

| 能力 | 参照实现 | Coding2API 现状 | 差距 |
|---|---|---|---|
| 多协议入口 | New API 覆盖 OpenAI/Claude/Gemini；CCR 覆盖 Gemini Generate Content | 仅 Chat、Responses、Messages（`src/api/`） | 缺 Gemini 原生入口与 `count_tokens` 之外的生态端点 |
| Key 治理 | One API 令牌额度+过期+IP/模型白名单；LiteLLM 虚拟 Key 预算周期 | 日配额 + 瞬时限速；无月度/总量预算、无 Key 到期 | 预算周期与生命周期不完整 |
| 可靠性路由 | Portkey 条件路由/负载均衡；LiteLLM Router；CCR 有序回退 | 健康度+权重+到期额度+fallback 组 | 缺按成本/延迟/时段的条件策略 |
| 成本 | LiteLLM 花费追踪/预算；New API 表达式定价 | OpenRouter 刊例静态价 + 实测扣费校准 | 价格表需手工更新，缺自动同步 |
| 缓存 | LiteLLM/Portkey 网关缓存；New API 缓存计费 | 仅透传上游 `prompt_cache_key`/`cache_control` | 无网关侧响应缓存 |
| 安全护栏 | LiteLLM/Portkey 请求/响应护栏 | 登录限流、CSRF、Host 白名单、日志脱敏 | 无内容/PII 护栏钩子 |
| 可观测 | LiteLLM/Portkey 标准导出；New API 审计+日志 | 进程内统计/SSE/告警/审计 | 无 Prometheus/OTel 标准导出 |
| 多租户 | One API 用户分组；New API 面向团队共享的配额/订阅 | 全局三角色，无团队维度 | 无团队级 Key/配额/账单 |
| 多节点 | New API Redis 共享限流/多节点 | 单实例锁 + SQLite，限流进程内 | 架构上不支持横向扩容 |
| 国际化 | New API 7 语言 | 管理台中文 | 无 i18n |
| 其他 | One API 充值/兑换码；Portkey MCP Gateway；CCR 桌面控制面/工具增强 | 内部共享池；已有 MCP 无关 | 商业/生态类能力不匹配定位 |

## 3. 候选功能清单（按优先级）

排序依据：先看内部用户（个人/小团队）的直接价值，再看实现成本，最后看与「单实例 + SQLite + 公共凭证池」架构的契合度。若后续确认转向对外多租户服务，团队隔离与护栏两项上调一档。

| # | 优先级 | 模块 | 候选功能 | 现状差距 | 建议 | 档位 |
|---|---|---|---|---|---|---|
| 1 | P0 | API 兼容层 | Gemini 原生入口 | 仅三协议 | 新增 `/v1beta/models/{model}:generateContent` 与 `:streamGenerateContent`，复用现有调度与统计；请求/响应映射放 `src/compat/gemini/` | 2 |
| 2 | P0 | 鉴权与配额 | Key 预算周期扩展 | 仅自然日配额 | `api_keys.quota` JSON 增 `monthly_*`、`total_*`、`expires_at`；准入判定与明细聚合复用 `key_quota.py` | 2 |
| 3 | P0 | 部署运维 | 标准可观测导出 | 仅内部页面/SSE | 增 `/metrics`（请求、token、成本、在途、凭证健康）并预留 OTel trace 导出；指标从现有统计层读取 | 2 |
| 4 | P1 | 安全边界 | 请求/响应护栏钩子 | 无内容治理 | 可插拔 `guardrail` 接口（内置敏感词/PII 规则，失败策略可配），默认关闭；命中记审计 | 3 |
| 5 | P1 | 调度与路由 | 条件路由策略 | 仅健康+权重 | 在现有排序前加规则表（按模型/渠道/成本/延迟/时段），规则存 DB、热更；保留现逻辑为默认策略 | 3 |
| 6 | P1 | 成本 | 模型价格表自动同步 | 刊例价静态 | 定时拉 LiteLLM/OpenRouter 价目，按模型归一键合并，冲突以实测扣费为准；价目快照留版本 | 2 |
| 7 | P1 | 统计计费 | 网关侧精确缓存 | 仅透传上游缓存 | 非流式按 `model+messages+tools` 哈希缓存，TTL 可配，命中不计上游用量；注意与「到期额度优先」策略的冲突，默认关闭 | 3 |
| 8 | P2 | 鉴权与配额 | 团队/租户隔离 | 全局三角色 | `teams` 表 + Key/统计/账单挂团队维度；迁移与权限模型改动大 | 3 |
| 9 | P2 | 管理端 | 管理台 i18n | 中文硬编码 | 抽词条表，先中/英双语；与现有模板/组件解耦 | 2 |

## 4. 不纳入本次范围

| 功能 | 理由 |
|---|---|
| 充值、兑换码、订阅计费 | 定位是内部公共凭证池，无对外收款场景；引入会带来支付与合规边界 |
| MCP Gateway / ToolHub | 与「编码订阅转 API」核心链路无关，且上游渠道不提供对应能力 |
| 桌面客户端 / 本地代理控制面 | 服务端已是控制面，再套一层只增加部署复杂度 |
| 多节点 / Redis 共享限流 | 现有单实例锁 + SQLite 是明确设计取舍；改造成本高于当前收益 |
| Embeddings / 图像 / 音频端点 | 十个逆向渠道以编码对话为主，端点覆盖与上游能力不匹配 |
| 单二进制打包 | Python 技术栈，Docker 镜像已覆盖交付形态 |
| 语义缓存 / Prompt 模板管理 | 编码场景命中率低，且模板管理属产品层能力，非网关缺口 |

## 5. 验证

本次仅新增本文档，生产代码零改动：

```bash
uv run pytest -q --cov=src --cov-report=term --cov-fail-under=100
pnpm --dir web exec vitest run
```