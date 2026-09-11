# coding2api

把 **CodeBuddy** 与 **TRAE SOLO** 两个 coding agent 上游通道，统一封装为 OpenAI 兼容 API，
并提供公共凭证池、统一调度与按人用量统计。

## 状态

早期开发中（M0 骨架）。当前进度见 `PROPOSAL.md` §9 里程碑。

| 文档 | 内容 |
|---|---|
| [`PROPOSAL.md`](PROPOSAL.md) | 立项决策（Q1–Q30）、可行性核实、风险清单 |
| [`TECHNICAL.md`](TECHNICAL.md) | 技术栈、模块规格、Provider 协议、请求时序、测试策略 |
| [`diagrams/coding2api-architecture.html`](diagrams/coding2api-architecture.html) | 系统架构图（浏览器直接打开） |

## 开源协议

MIT，见 [LICENSE](LICENSE)。借鉴的上游项目署名见 [NOTICE](NOTICE)。

> 本仓库接通的是第三方服务的逆向接口，仅供学习研究，请勿用于生产用途。
