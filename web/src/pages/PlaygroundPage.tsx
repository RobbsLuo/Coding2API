import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import {
  Check,
  Loader2,
  Send,
  TerminalSquare,
  X,
} from "lucide-react";
import { useSessionContext } from "../Layout";
import type { ModelInfo } from "../api/types";
import { HelpBlock } from "../components/HelpBlock";
import { PageHeader } from "../components/PageHeader";
import { ProviderIcon } from "../components/ProviderIcon";
import { ModelPicker, modelValue, pickDefaultModel } from "../components/ModelPicker";
import { Button, Card, Checkbox, Empty, Field, Label, Notice, Panel, Textarea } from "../ui";
import { providerLabel, providerRank } from "../api/providers";

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

  // 每个模型的受控值：单渠道模型带 @provider（该组选项即强制指定），
  // 多渠道模型用裸 id（走自动路由）。
  const models = fetched.map((item) => ({ ...item, value: modelValue(item) }));
  const selectedModel = model || pickDefaultModel(models);
  // 选中模型的元数据：优先精确匹配 value，退化为按小写基础名匹配
  const selectedInfo =
    models.find((item) => item.value === selectedModel) ??
    models.find((item) => item.value === selectedModel.split("@")[0]);

  // 强制指定渠道：只列该模型**真实可用**的渠道，避免选出上游打不通的
  // model@provider。单渠道模型只有一个选项（始终固定）。
  const selectedProviders = selectedInfo
    ? [...selectedInfo.providers].sort((left, right) => providerRank(left) - providerRank(right))
    : [];
  const pinnedProvider = selectedModel.includes("@") ? selectedModel.split("@")[1] : "";

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
        icon={<TerminalSquare className="size-5" />}
      />

      <HelpBlock
        title="模型与渠道调度说明"
        entries={[
          { term: "模型名 model@provider", where: "模型 · 强制指定渠道", meaning: "默认 glm-5.2 由调度器在可用渠道间自动选健康的；写 glm-5.2@trae 则只走 TRAE，写 模型@zen 只走 OpenCode Zen。" },
        ]}
      />

      <Panel title="请求">
        <form onSubmit={send} className="space-y-3">
          <Field label="模型" hint="多渠道模型默认自动调度；需要时可在下方强制指定渠道">
            <ModelPicker
              models={models}
              value={selectedModel}
              onChange={setModel}
              loading={modelsQuery.isFetching && fetched.length === 0}
            />
          </Field>

          {selectedProviders.length > 1 && (
            <Field label="强制指定渠道" hint="仅对当前模型可用的渠道生效">
              <div className="flex flex-wrap items-center gap-2" data-testid="provider-pin">
                <button
                  type="button"
                  data-testid="provider-pin-auto"
                  aria-pressed={pinnedProvider === ""}
                  onClick={() => setModel(selectedModel.split("@")[0])}
                  className={
                    "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-colors "
                    + (pinnedProvider === ""
                      ? "border-primary/40 bg-primary/10 text-primary"
                      : "border-border text-muted-foreground hover:text-foreground")
                  }
                >
                  自动路由
                </button>
                {selectedProviders.map((provider) => (
                  <button
                    key={provider}
                    type="button"
                    data-testid={`provider-pin-${provider}`}
                    aria-pressed={pinnedProvider === provider}
                    onClick={() => setModel(`${selectedModel.split("@")[0]}@${provider}`)}
                    className={
                      "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-colors "
                      + (pinnedProvider === provider
                        ? "border-primary/40 bg-primary/10 text-primary"
                        : "border-border text-muted-foreground hover:text-foreground")
                    }
                  >
                    <ProviderIcon provider={provider} size={13} />
                    {providerLabel(provider)}
                  </button>
                ))}
              </div>
            </Field>
          )}

          {selectedInfo && (selectedInfo.max_input_tokens !== undefined ||
            selectedInfo.max_output_tokens !== undefined ||
            selectedInfo.supports_images !== undefined ||
            selectedInfo.supports_tool_call !== undefined) && (
            <Card
              size="sm"
              className="flex flex-row flex-wrap items-center gap-x-4 gap-y-1 bg-muted/40 px-3 py-2 text-xs text-muted-foreground ring-border"
              data-testid="model-meta"
            >
              {/* 渠道与倍率由模型选择器的「当前选择」条展示，这里只补齐能力/上限 */}
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
            </Card>
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
          <Label className="w-fit cursor-pointer gap-2 text-sm font-normal">
            <Checkbox
              checked={stream}
              data-testid="stream-toggle"
              onCheckedChange={(value) => setStream(value === true)}
            />
            流式输出
          </Label>
          <Button type="submit" variant="primary" disabled={busy} data-testid="send-request" className="gap-1.5">
            {busy ? <Loader2 className="size-4 animate-spin" /> : <Send className="size-4" />}
            {busy ? "请求中…" : "发送"}
          </Button>
        </form>
      </Panel>

      {(error || modelsQuery.error) && (
        <div data-testid="playground-error" role="alert">
          <Notice tone="danger">
            {error ?? "模型列表加载失败，请刷新重试"}
          </Notice>
        </div>
      )}

      {/* 请求/流式响应无焦点变化：aria-live 让读屏可感知回答到达 */}
      <div aria-live="polite">
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
            <Card
              size="sm"
              className="mt-3 gap-0 bg-muted/40 px-3 py-2 font-mono text-xs text-muted-foreground ring-border"
              data-testid="playground-usage"
            >
              <pre className="whitespace-pre-wrap">{JSON.stringify(usage, null, 2)}</pre>
              <div className="mt-1 font-sans">
                credit 为渠道可选字段，经常不返回；健康度只依赖额度探测接口。
              </div>
            </Card>
          )}
        </Panel>
      )}
      </div>

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