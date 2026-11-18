"""Gemini（`generateContent` / `streamGenerateContent`）兼容层。

只做**形状转换**：入站把 Gemini 请求体映射成内部 chat 载荷，出站把中立
`Event` / 聚合 completion 映射成 Gemini 的 `GenerateContentResponse`。选号、
冷却、轮换、统计、会话粘性全部复用既有引擎，与另外三个出口一致。
"""
