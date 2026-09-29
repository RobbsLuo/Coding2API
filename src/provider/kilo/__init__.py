"""Kilo Gateway 免费层渠道。

Kilo Gateway（`api.kilo.ai/api/gateway`）对外是**标准 OpenAI 兼容协议**
（`/chat/completions` + `/models`），无需像 CodeBuddy / TRAE 那样逆向私有
信封，也不像 Zen 那样有免费层门禁伪装。本包只承担三件渠道私有的事：

1. 免费模型识别：`/models` 每个条目带权威 `isFree` 布尔（实测 395 个模型
   中 17 个 `isFree=true`），据此过滤即可，**不做探活**（见 `client.py`
   说明：探活会白白消耗本就极小的免费配额，且结果不稳定）；
2. 无凭证：和 Zen 一样，池里放一条虚拟凭证即可复用现有调度/冷却/统计；
3. 无额度接口：`probe_quota` 恒返回「未知」而非「耗尽」。

实测（2026-09-29）：免费模型匿名可用（无需 key）；上游限流按 **200 请求/小时/IP**
（网关级），且免费池实为 OpenRouter 免费池的转发（429 报错原文含
`limit_source: openrouter_shared_capacity`），故免费模型会随 OpenRouter 池
波动——上游 429 由引擎按 `SOFT` 软冷却触发换模型，本包不自建熔断。
"""
