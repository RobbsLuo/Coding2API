"""OpenCode Zen 免费层渠道。

Zen 是标准 OpenAI 兼容协议（`/zen/v1/chat/completions`），不需要像
CodeBuddy / TRAE 那样逆向私有信封；本包只承担三件渠道私有的事：

1. 免费层门禁伪装（UA 版本 + `x-opencode-session` + `stream:true` +
   tools 必须同时含 `bash`/`read`），见 `client.prepare_body`；
2. 无凭证：池里放一条虚拟凭证即可复用现有调度/冷却/统计；
3. 无额度接口：`probe_quota` 恒返回「未知」而非「耗尽」。
"""
