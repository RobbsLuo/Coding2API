import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import {
  Check,
  Cloud,
  Loader2,
  RefreshCw,
  Send,
  X,
  Zap,
} from "lucide-react";
import { useSessionContext } from "../Layout";
import { HelpBlock } from "../components/HelpBlock";
import { PageHeader } from "../components/PageHeader";
import { Button, Empty, Field, Notice, Panel, Select, Textarea } from "../ui";

interface ModelInfo {
  id: string;
  object: string;
  owned_by: string;
  providers: string[];
  credit_rate?: number;
  max_input_tokens?: number;
  max_output_tokens?: number;
  supports_images?: boolean;
  supports_tool_call?: boolean;
  by_provider?: Record<string, { credit_rate?: number }>;
}

const PROVIDER_LABEL: Record<string, string> = {
  codebuddy: "CodeBuddy",
  trae: "TRAE",
};

// 渠道前置 icon 与颜色（与统计页图例同色系：CB 蓝 / TRAE 橙）
const CHANNEL_ICON: Record<string, typeof Cloud> = { codebuddy: Cloud, trae: Zap };
const CHANNEL_COLOR: Record<string, string> = {
  codebuddy: "text-[var(--chart-1)]",
  trae: "text-[var(--chart-3)]",
};

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
  const fetched = modelsQuery.data ?? [];

  // 每个模型的受控值：单上游模型带 @provider（该组选项即强制指定），
  // 双上游模型用裸 id（走自动路由）。派生选中时必须用同一套值，
  // 否则受控值与任何 option 都对不上，select 会显示为空。
  const valueOf = (item: { id: string; providers: string[] }): string =>
    item.providers.length === 1 ? `${item.id}@${item.providers[0]}` : item.id;
  const models = fetched.map((item) => ({ ...item, value: valueOf(item) }));
  const selectedModel = model || (models[0]?.value ?? "");
  // 选中模型的元数据：优先精确匹配 value，退化为按小写基础名匹配
  const selectedInfo =
    models.find((item) => item.value === selectedModel) ??
    models.find((item) => item.value === selectedModel.split("@")[0]);
  const dualSource = models.filter((item) => item.providers.length > 1);
  const onlyCodebuddy = models.filter(
    (item) => item.providers.length === 1 && item.providers[0] === "codebuddy",
  );
  const onlyTrae = models.filter(
    (item) => item.providers.length === 1 && item.providers[0] === "trae",
  );
  void valueOf;
  const groups: [string, typeof onlyCodebuddy][] = [];
  if (onlyCodebuddy.length) groups.push(["codebuddy", onlyCodebuddy]);
  if (onlyTrae.length) groups.push(["trae", onlyTrae]);

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
          model: selectedModel,
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
      <PageHeader
        title="Playground"
        description="用登录会话直接测试模型调度，无需 API Key；请求会走与外部 API 相同的调度与统计，用量计入当前用户。"
      />

      <HelpBlock
        title="模型与上游调度说明"
        entries={[
          { term: "模型名 model@provider", where: "模型 · 强制指定上游", meaning: "默认 glm-5.2 由调度器在两个上游间自动选健康的；写 glm-5.2@trae 则只走 TRAE，glm-5.2@codebuddy 只走 CodeBuddy。" },
        ]}
      />

      <Panel title="请求">
        <form onSubmit={send} className="space-y-3">
          <div className="grid items-start gap-3 sm:grid-cols-[minmax(0,20rem)_minmax(0,16rem)]">
              <Field
                label="模型"
                hint="自动选健康上游；写 model@provider 可强制指定"
              >
                <Select
                  value={selectedModel}
                  data-testid="model-select"
                  onChange={(event) => setModel(event.target.value)}
                >
                  <option value="">选择模型…</option>
                  {/* 双上游可用的模型置顶：默认调度即可覆盖 */}
                  {dualSource.length > 0 && (
                    <optgroup label="双上游（自动调度）">
                      {dualSource.map((item) => (
                        <option key={item.id} value={item.id}>
                          {item.id}（自动路由）
                        </option>
                      ))}
                    </optgroup>
                  )}
                  {groups.map(([provider, items]) => (
                    <optgroup key={provider} label={`仅 ${PROVIDER_LABEL[provider]}`}>
                      {items.map((item) => (
                        <option key={item.id} value={item.value}>
                          {item.id}
                        </option>
                      ))}
                    </optgroup>
                  ))}
                  {/* model@provider 组合不在原始列表里，必须补合成选项，
                      否则 React 会把 select 渲染成无选中项。
                      单上游模型的 value 本身带 @provider 且已在列表里，
                      不重复合成，避免下拉显示成「xxx@trae（强制指定）」。 */}
                  {selectedModel.includes("@") &&
                    !models.some((item) => item.value === selectedModel) && (
                    <option value={selectedModel}>{selectedModel}（强制指定）</option>
                  )}
                </Select>
              </Field>
              <Field label="强制指定上游" hint="双上游模型可用；单上游模型始终固定">
                <Select
                  value={selectedModel.includes("@") ? selectedModel.split("@")[1] : ""}
                  data-testid="provider-pin"
                  onChange={(event) => {
                    const base = selectedModel.split("@")[0];
                    setModel(event.target.value ? `${base}@${event.target.value}` : base);
                  }}
                >
                  <option value="">自动路由</option>
                  <option value="codebuddy">CodeBuddy</option>
                  <option value="trae">TRAE</option>
                </Select>
              </Field>
          </div>
          {selectedInfo && (selectedInfo.credit_rate !== undefined ||
            selectedInfo.max_input_tokens !== undefined ||
            selectedInfo.max_output_tokens !== undefined ||
            selectedInfo.supports_images !== undefined ||
            selectedInfo.supports_tool_call !== undefined) && (
            <div
              className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground"
              data-testid="model-meta"
            >
              {/* 渠道：前置 icon 标注，多渠道全部显示 */}
              <span className="inline-flex items-center gap-1.5">
                渠道
                {(selectedInfo.providers ?? []).map((pid) => {
                  const Icon = CHANNEL_ICON[pid];
                  return (
                    <span
                      key={pid}
                      className="inline-flex items-center gap-1 font-medium text-foreground"
                    >
                      {Icon && <Icon className={"size-3.5 " + (CHANNEL_COLOR[pid] ?? "")} />}
                      {PROVIDER_LABEL[pid] ?? pid}
                    </span>
                  );
                })}
              </span>
              {/* 倍率：多渠道且各不相同则按渠道分别显示 */}
              {selectedInfo.providers && selectedInfo.providers.length > 1 && selectedInfo.by_provider ? (
                selectedInfo.providers.map((pid) => {
                  const rate = selectedInfo.by_provider?.[pid]?.credit_rate;
                  const Icon = CHANNEL_ICON[pid];
                  return rate === undefined ? null : (
                    <span key={pid} className="inline-flex items-center gap-1">
                      {Icon && <Icon className={"size-3.5 " + (CHANNEL_COLOR[pid] ?? "")} />}
                      倍率 <span className="font-medium tabular-nums text-foreground">x{rate}</span>
                    </span>
                  );
                })
              ) : (
                selectedInfo.credit_rate !== undefined && (
                  <span>
                    消耗倍数 <span className="font-medium tabular-nums text-foreground">
                      x{selectedInfo.credit_rate}
                    </span>
                  </span>
                )
              )}
              {selectedInfo.max_input_tokens !== undefined && (
                <span>
                  最大输入 <span className="font-medium tabular-nums text-foreground">
                    {selectedInfo.max_input_tokens.toLocaleString()}
                  </span>
                </span>
              )}
              {selectedInfo.max_output_tokens !== undefined && (
                <span>
                  最大输出 <span className="font-medium tabular-nums text-foreground">
                    {selectedInfo.max_output_tokens.toLocaleString()}
                  </span>
                </span>
              )}
              {selectedInfo.supports_images !== undefined && (
                <span className="inline-flex items-center gap-1">
                  图片
                  {selectedInfo.supports_images
                    ? <Check className="size-3.5 text-ok" />
                    : <X className="size-3.5 text-destructive" />}
                </span>
              )}
              {selectedInfo.supports_tool_call !== undefined && (
                <span className="inline-flex items-center gap-1">
                  工具调用
                  {selectedInfo.supports_tool_call
                    ? <Check className="size-3.5 text-ok" />
                    : <X className="size-3.5 text-destructive" />}
                </span>
              )}
            </div>
          )}
          {modelsQuery.isFetching && (
            <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
              <RefreshCw className="size-3 animate-spin" />
              载入模型中…
            </span>
          )}
          <Field label="提示词">
            <Textarea
              rows={4}
              value={prompt}
              data-testid="playground-prompt"
              className="font-mono text-xs"
              onChange={(event) => setPrompt(event.target.value)}
            />
          </Field>
          <label className="flex w-fit cursor-pointer select-none items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={stream}
              className="size-4 accent-primary"
              data-testid="stream-toggle"
              onChange={(event) => setStream(event.target.checked)}
            />
            流式输出
          </label>
          <Button type="submit" variant="primary" disabled={busy} data-testid="send-request" className="gap-1.5">
            {busy ? <Loader2 className="size-4 animate-spin" /> : <Send className="size-4" />}
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
              <div className="mb-1 inline-flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
                <span className="rounded px-1 py-0.5 font-mono bg-muted">reasoning</span>
                思考链
              </div>
              <pre className="rounded-lg border border-border bg-foreground/[0.03] p-3 text-xs whitespace-pre-wrap">
                {reasoning}
              </pre>
            </div>
          )}
          <div data-testid="playground-answer">
            <div className="mb-1 text-xs text-muted-foreground">回答</div>
            <pre className="rounded-lg border border-border bg-foreground/[0.03] p-3 text-xs whitespace-pre-wrap">
              {answer}
            </pre>
          </div>
          {usage && (
            <div
              className="mt-3 rounded-lg border border-border bg-muted/40 px-3 py-2 font-mono text-xs text-muted-foreground"
              data-testid="playground-usage"
            >
              <pre className="whitespace-pre-wrap">{JSON.stringify(usage, null, 2)}</pre>
              <div className="mt-1 font-sans">
                credit 为上游可选字段，经常不返回；健康度只依赖额度探测接口。
              </div>
            </div>
          )}
        </Panel>
      )}

      {!answer && !reasoning && !error && (
        <Empty>
          <div className="space-y-1">
            <p>选择模型并填写提示词后发送，结果会显示在下方。</p>
            <p className="text-xs">
              用量计入 {session.username}，与外部 API 同一条调度与统计链路。
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
