import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useSessionContext } from "../Layout";
import { Button, Empty, Field, Notice, Panel, Select, Textarea } from "../ui";

interface ModelInfo {
  id: string;
  object: string;
  owned_by: string;
  providers: string[];
}

/** 通过会话鉴权的内部端点取数据，不需要用户自己造 API Key。 */
async function fetchPlaygroundModels(signal?: AbortSignal) {
  const response = await fetch("/api/playground/models", { credentials: "same-origin", signal });
  if (!response.ok) throw new Error("模型列表加载失败");
  const body = (await response.json()) as { data: ModelInfo[] };
  return body.data;
}

export function PlaygroundPage() {
  const session = useSessionContext();
  const [model, setModel] = useState("");
  const [prompt, setPrompt] = useState("");
  const [stream, setStream] = useState(true);
  const [answer, setAnswer] = useState("");
  const [reasoning, setReasoning] = useState("");
  const [usage, setUsage] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const modelsQuery = useQuery({
    queryKey: ["playground-models"],
    queryFn: ({ signal }) => fetchPlaygroundModels(signal),
  });
  const models = modelsQuery.data ?? [];

  // 模型载入后自动选中第一个，省一次无意义的手动选择。
  // 只依赖数量：models 引用每次渲染都变，会导致 effect 反复触发。
  const modelCount = models.length;
  const hasModel = model !== "";
  useEffect(() => {
    if (modelCount > 0 && !hasModel) {
      setModel(models[0].id);
    }
  }, [modelCount, hasModel, models]);

  const send = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setAnswer("");
    setReasoning("");
    setUsage(null);
    try {
      const response = await fetch("/api/playground/chat/completions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          model,
          messages: [{ role: "user", content: prompt }],
          stream,
        }),
      });
      if (response.status === 401) {
        // 会话过期：提示重新登录而不是一句原始错误
        window.location.href = "/login";
        return;
      }
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
      <Panel title="请求">
        <form onSubmit={send} className="space-y-3">
          <div className="flex flex-wrap items-end gap-3">
            <div className="w-72">
              <Field
                label="模型"
                hint="自动选健康上游；写 model@provider 可强制指定"
              >
                <Select
                  value={model}
                  data-testid="model-select"
                  onChange={(event) => setModel(event.target.value)}
                >
                  <option value="">选择模型…</option>
                  {models.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.id}（{item.providers.join(" / ")}）
                    </option>
                  ))}
                  {/* model@provider 组合不在原始列表里，必须补合成选项，
                      否则 React 会把 select 渲染成无选中项 */}
                  {model.includes("@") && (
                    <option value={model}>{model}（强制指定）</option>
                  )}
                </Select>
              </Field>
            </div>
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
            {modelsQuery.isFetching && (
              <span className="text-xs text-[var(--color-ink-muted)]">载入模型中…</span>
            )}
          </div>
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

      {(error || modelsQuery.error) && (
        <div data-testid="playground-error">
          <Notice tone="danger">
            {error ?? "模型列表加载失败，请刷新重试"}
          </Notice>
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

      {!answer && !reasoning && !error && (
        <Empty>
          <div className="space-y-1">
            <p>填写提示词后发送，请求会走与外部 API 相同的调度与统计。</p>
            <p className="text-xs">
              无需 API Key——这里用的是你的登录会话，用量计入 {session.username}。
              API Key 仅供外部客户端接入使用。
            </p>
          </div>
        </Empty>
      )}
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
