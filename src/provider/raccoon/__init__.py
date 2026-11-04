"""小浣熊（商汤 Raccoon）渠道。

上游是**标准 OpenAI 兼容协议**，没有私有信封、没有 COSY 签名、没有设备指纹：

* 推理 `POST https://xiaohuanxiong.com/api/web/llm/v2/chat/completions`
* 模型目录 `GET /api/web/llm/v2/model_catalog`
* 积分余额 `GET /api/web/points/v1/balance`

凭证是**网页会话 JWT**（cookie `xhx_token`，`iss=xiaohuanxiong.com/iam`，
有效期约 30 天），直接当 `Authorization: Bearer` 使用。本包只承担四件渠道
私有的事：

1. 凭证归一：从 JWT / `{"access_token": …}` / 整段 cookie 串里取出 token，
   并从 claims 里解出 `uid` / 过期时间（不验签，只当元数据）；
2. 免费模型识别：目录条目带 `billing_effective_multiplier`，为 0 即当免费
   （`credit_rate=0`，调度器据此优先选它），非 0 透传成倍率；
3. **未知模型防护**：上游对不认识的模型名会**静默回落到默认模型并返回 200**
   （2026-11-04 实测 `definitely-not-a-model` → 200 + `model=raccoon-090dd3`）。
   本包在流首帧比对上游回显的 `model`，不一致立即报协议违规，绝不让客户端
   拿到「问 A 答 B」的结果；
4. 额度口径：积分按**请求**扣（每笔 `points=-1`，与 token 量无关），故
   `probe_quota` 只报余额，不做 token 换算。
"""