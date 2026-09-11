import { useState } from "react";
import { api } from "../api/client";
import type { ModelInfo } from "../api/types";
import { Button, Empty, Field, Input, Notice, Panel, Select, Textarea } from "../ui";

export function PlaygroundPage() {
  const [apiKey, setApiKey] = useState("");
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [model, setModel] = useState("");
  const [prompt, setPrompt] = useState("");
  const [stream, setStream] = useState(true);
  const [answer, setAnswer] = useState("");
  const [reasoning, setReasoning] = useState("");
  const [usage, setUsage] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const loadModels = async () => {
    setError(null);
    try {
      const result = await api.models(apiKey);
      setModels(result.data);
      setModel(result.data[0]?.id ?? "");
    } catch {
      setError("模型列表加载失败，请确认 API Key 有效");
    }
  };

  const send = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setAnswer("");
    setReasoning("");
    setUsage(null);
    try {
      const response = await api.chatCompletion(apiKey, {
        model,
        messages: [{ role: "user", content: prompt }],
        stream,
      });
      if (!response.ok) {
        const body = (await response.json()) as { error?: { message?: string } };
        setError(body.error?.message ?? `请求失败（${response.status}）`);
        return;
      }
      if (stream) {
        await readStream(response, { setAnswer, setReasoning, setError });
      } else {
        const body = (await response.json()) as {
          choices: { message: { content: string | null; reasoning_content?: string } }[];
          usage?: Record<string, unknown>;
        };
        const message = body.choices[0]?.message;
        setAnswer(message?.content ?? "");
        setReasoning(message?.reasoning_content ?? "");
        setUsage(body.usage ?? null);
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "请求失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-6" data-testid="playground-page">
      <Panel title="连接">
        <div className="flex flex-wrap items-end gap-3">
          <div className="w-72">
            <Field label="API Key" hint="仅保存在本页内存，不写入 localStorage">
              <Input
                type="password"
                value={apiKey}
                data-testid="playground-key"
                placeholder="sk-..."
                onChange={(event) => setApiKey(event.target.value)}
              />
            </Field>
          </div>
          <Button onClick={() => void loadModels()} data-testid="load-models">
            载入模型
          </Button>
          {models.length > 0 && (
            <div className="w-64">
              <Field label="模型" hint="可用 model@provider 强制指定上游">
                <Select
                  value={model}
                  data-testid="model-select"
                  onChange={(event) => setModel(event.target.value)}
                >
                  {models.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.id}（{item.providers.join(" / ")}）
                    </option>
                  ))}
                  {/* 强制指定上游后当前值形如 model@provider，不在原始列表里，
                      必须补一个合成选项，否则 select 会显示为空白 */}
                  {model.includes("@") && (
                    <option value={model}>{model}（强制指定）</option>
                  )}
                </Select>
              </Field>
            </div>
          )}
          {models.length > 0 && (
            <div className="w-56">
              <Field label="强制指定上游">
                <Select
                  value={model.includes("@") ? model.split("@")[1] : ""}
                  data-testid="provider-pin"
                  onChange={(event) => {
                    const base = model.split("@")[0];
                    setModel(event.target.value ? `${base}@${event.target.value}` : base);
                  }}
                >
                  <option value="">自动路由</option>
                  <option value="codebuddy">CodeBuddy</option>
                  <option value="trae">TRAE</option>
                </Select>
              </Field>
            </div>
          )}
        </div>
      </Panel>

      <Panel title="请求">
        <form onSubmit={send} className="space-y-3">
          <Field label="提示词">
            <Textarea
              rows={4}
              value={prompt}
              data-testid="playground-prompt"
              onChange={(event) => setPrompt(event.target.value)}
            />
          </Field>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={stream}
              data-testid="stream-toggle"
              onChange={(event) => setStream(event.target.checked)}
            />
            流式输出
          </label>
          <Button type="submit" variant="primary" disabled={busy} data-testid="send-request">
            {busy ? "请求中…" : "发送"}
          </Button>
        </form>
      </Panel>

      {error && (
        <div data-testid="playground-error">
          <Notice tone="danger">{error}</Notice>
        </div>
      )}

      {(answer || reasoning) && (
        <Panel title="响应">
          {reasoning && (
            <div className="mb-3" data-testid="playground-reasoning">
              <div className="mb-1 text-xs text-[var(--color-ink-muted)]">思考链</div>
              <pre className="rounded-lg border border-[var(--color-border-soft)] bg-[var(--color-surface)] p-3 text-xs whitespace-pre-wrap">
                {reasoning}
              </pre>
            </div>
          )}
          <div data-testid="playground-answer">
            <div className="mb-1 text-xs text-[var(--color-ink-muted)]">回答</div>
            <pre className="rounded-lg border border-[var(--color-border-soft)] bg-[var(--color-surface)] p-3 text-xs whitespace-pre-wrap">
              {answer}
            </pre>
          </div>
          {usage && (
            <div className="mt-3 text-xs text-[var(--color-ink-muted)]" data-testid="playground-usage">
              {JSON.stringify(usage)}
              <div className="mt-1">
                credit 为上游可选字段，经常不返回；健康度只依赖额度探测接口。
              </div>
            </div>
          )}
        </Panel>
      )}

      {!answer && !reasoning && !error && <Empty>还没有响应</Empty>}
    </div>
  );
}

async function readStream(
  response: Response,
  sink: {
    setAnswer: (updater: (previous: string) => string) => void;
    setReasoning: (updater: (previous: string) => string) => void;
    setError: (message: string) => void;
  },
): Promise<void> {
  const reader = response.body?.getReader();
  if (!reader) return;
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let index = buffer.indexOf("\n");
    while (index >= 0) {
      const line = buffer.slice(0, index).trim();
      buffer = buffer.slice(index + 1);
      index = buffer.indexOf("\n");
      if (!line.startsWith("data:")) continue;
      const payload = line.slice(5).trim();
      if (payload === "[DONE]") return;
      try {
        const chunk = JSON.parse(payload) as {
          choices?: { delta?: { content?: string | null; reasoning_content?: string | null } }[];
          error?: { message?: string };
        };
        if (chunk.error?.message) {
          sink.setError(chunk.error.message);
          return;
        }
        const delta = chunk.choices?.[0]?.delta;
        if (delta?.reasoning_content) {
          const text = delta.reasoning_content;
          sink.setReasoning((previous) => previous + text);
        }
        if (delta?.content) {
          const text = delta.content;
          sink.setAnswer((previous) => previous + text);
        }
      } catch {
        // 非 JSON 帧（心跳等）忽略
      }
    }
  }
}
