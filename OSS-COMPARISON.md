# 同类开源项目功能对照与候选升级清单

调研时间：2026-11-12。对照对象为 5 个自托管 LLM 网关 / 多凭据聚合项目，版本与更新时间取自各仓库 GitHub Release 与仓库元数据。本文只做差距分析，不改动生产代码。

## 1. 对照对象

| 项目 | 版本 / 更新时间 | 定位 | 与本项目重叠 |
|---|---|---|---|
| [songquanpeng/one-api](https://github.com/songquanpeng/one-api) | v0.6.10 / 2025-02-02 | 多供应商 Key 管理与二次分发，单二进制 | 渠道池、Key 配额、模型重定向、兑换码、OAuth 登录 |
| [Calcium-Ion/new-api](https://github.com/Calcium-Ion/new-api) | v1.0.0-rc.49 / 2026-11-08 | 商业向 AI 网关，OpenAI / Claude / Gemini 三协议 | 渠道分组、模型映射、计费、看板、OAuth、订阅 |
| [BerriAI/litellm](https://github.com/BerriAI/litellm) | v1.109.0 / 2026-11-11 | 100+ 供应商 SDK + 网关 | 路由回退、虚拟 Key 预算、缓存、护栏、可观测性 |
| [yym68686/uni-api](https://github.com/yym68686/uni-api) | v1.7.276 / 2026-09-12 | YAML 配置的轻量多供应商聚合 | 统一 OpenAI 出口、负载均衡、重试、限流 |
| [tbphp/gpt-load](https://github.com/tbphp/gpt-load) | v2.0.0-rc.63 / 2026-11-08 | 多渠道多凭据自托管网关 | 密钥/订阅账号池、加权调度、故障转移、请求日志与用量 |

## 2. 逐项对照

| 能力 | 本项目现状 | 同类项目做法 | 结论 |
|---|---|---|---|
| 供应商接入 | 硬编码 10 个订阅渠道（`KNOWN_PROVIDERS`） | one-api / new-api / uni-api / LiteLLM 均允许任意 OpenAI 兼容 `base_url` | **缺口**，见 C1 |
| 模型映射 | 全局 `MODEL_ALIASES`，渠道内模型名由代码归一 | one-api / new-api 支持渠道级「模型重定向」 | **部分缺口**，见 C3 |
| 调度与容错 | 健康度、权重、冷却、到期额度、粘性会话、回退组 | LiteLLM Router、new-api 渠道分组、gpt-load 加权轮询 | 本项目不弱，保持 |
| 配额治理 | Key / 用户双层的日请求、日 token、日成本、RPM、TPM、并发；凭证护栏 | 竞品多为 Key / 用户 / 团队预算，部分支持 Redis 跨副本 | 本项目够用；团队层级见第 4 节 |
| 鉴权 | 本地账号、一次性激活、三角色、CSRF、IP / 模型白名单 | one-api / new-api 支持 GitHub / OIDC 等 OAuth | **缺口**，见 C4 |
| 可观测性 | Prometheus `/metrics`、环形日志、审计、九类告警 + Webhook | LiteLLM 接 OTel / Langfuse；new-api 数据看板 | **部分缺口**，见 C5 |
| 成本核算 | OpenRouter 刊例价 + 24h 实测扣费校准 | LiteLLM 维护价格表；new-api 支持自定义倍率 | **部分缺口**，见 C6 |
| 协议兼容 | OpenAI Chat / Responses、Claude Messages、模型列表 | new-api 另有原生 Gemini；LiteLLM 全协议 | **低优先缺口**，见 C7 |
| 运维动作 | 批量探活/签到/预刷新、备份、配置热更 85 项 | gpt-load / one-api 批量导入导出、渠道测试 | 本项目覆盖较全 |
| 计费与充值 | 不做按 token 计费（订阅制上游，见 PROPOSAL 非目标） | one-api / new-api 完整计费、兑换码、充值 | 不纳入，见第 4 节 |
| 对话留存 | 明确不存对话，只留用量明细 | 多数竞品记录请求/响应全文 | 不纳入，见第 4 节 |

## 3. 候选功能清单（按优先级）

优先级依据：用户价值（能否直接解决渠道可用性 / 运维成本 / 账单准确性）> 与现有架构契合度 > 实现成本。工作量档位：1 = ≤2 人天，2 = 3–7 人天，3 = ≥8 人天或跨模块改造。

| 优先级 | 编号 | 候选功能 | 所属模块 | 现状差距 | 升级 / 新增建议 | 档位 |
|---|---|---|---|---|---|---|
| P0 | C1 | 自定义 OpenAI 兼容渠道 | provider / credentials / 调度 | 新增供应商必须改代码与发版 | 新增 `generic_openai` 适配器：admin 配 `base_url`、密钥、模型列表；复用现有 executor、统计、冷却与配额 | 3 |
| P0 | C2 | 标签参与调度与限额 | engine / db / 管理台 | 标签仅展示，不参与选号或限额 | 候选过滤支持按标签（如仅 `prod`）；凭证限额支持按标签聚合日用量 | 2 |
| P1 | C3 | 渠道级模型 ID 覆盖 | provider / model_resolver | 上游改模型 ID 需改代码；全局别名无法按渠道区分 | 增加 `provider → {统一名: 上游 ID}` 配置，解析优先级：渠道覆盖 > 全局别名 > 原值 | 1 |
| P1 | C4 | OIDC / SSO 登录 | auth / 管理台 | 只有本地账号与激活链接 | OIDC 授权码登录，claim 映射到现有三角色；保留本地账号兜底 | 2 |
| P1 | C5 | OpenTelemetry 链路导出 | observability | 有 request_id 与 Prometheus，但无 trace 导出 | 可选 OTLP exporter，复用 request_id 贯穿上游调用，支持采样与关闭 | 2 |
| P2 | C6 | 模型单价手工覆盖 | 统计 / 成本 | 成本依赖 OpenRouter 刊例价与实测校准，无法按合同价修正 | admin 可编辑每模型 USD/1M 单价，未配置时回落现有校准逻辑 | 1 |
| P2 | C7 | 原生 Gemini 入口 | api / compat | 仅 OpenAI 与 Claude 入站 | `/v1beta/models/*:generateContent` 入站翻译，复用现有三出口骨架 | 2 |

## 4. 不纳入本次范围

| 功能 | 理由 |
|---|---|
| 按 token 计费、充值、兑换码、套餐订阅 | PROPOSAL 明确非目标：上游订阅制，成本不随 token 线性增长 |
| 多租户组织 / 团队层级 | 目标用户尚未定为团队或多租户（PO 待确认）；现有 users + 三角色已覆盖单团队 |
| 请求 / 响应全文留存与回放 | 现有隐私设计明确不存对话；竞品做法不适用于本定位 |
| 语义缓存 / Prompt 缓存 | 会破坏用量与成本口径，订阅制下收益不明确 |
| Embeddings / Rerank / 图片 / 音频端点 | 十个上游以编码类 chat 为主，短期无真实需求 |
| 分布式限流与多副本 | 现架构为单实例锁 + SQLite；需先重构存储层，成本高于收益 |
| MCP 网关 / Agent 托管 | 本项目定位是 API 出口，不是 Agent 平台 |

## 5. 建议排期

先做 C2（成本低、直接改善多号运维），再评估 C1 与 C4；C3、C6 可作为顺手项并入相邻迭代；C5、C7 等真实需求出现再排。