import { useRef, useState } from "react";
import {
  Check,
  ImagePlus,
  Loader2,
  Send,
  TerminalSquare,
  X,
} from "lucide-react";
import { useSessionContext } from "../Layout";
import { usePlaygroundModels } from "../api/hooks";
import { notifyUnauthorized } from "../api/unauthorized";
import { errorText } from "../lib/errors";
import { PageHeader } from "../components/PageHeader";
import { ModelPicker, modelValue, pickDefaultModel } from "../components/ModelPicker";
import { buildSamplingParams, effortOptions } from "../lib/playgroundParams";
import { Button, Card, Checkbox, Empty, Field, Input, Label, Notice, Panel, Select, Textarea } from "../ui";

/**
 * Playground 请求超时。
 *
 * 流式回答可达数分钟（长推理 + 图片），故取 120s；并且**只约束「响应头 / 首字节
 * 之前」**：进入读流阶段后清掉定时器，否则会把正常的长时间流式回答截断。
 * 取舍：首字迟迟不来视为后端挂死（连接池耗尽、上游不返回），宁可报错也不让
 * `busy` 永不复位；一旦开始出字，就让流跑完。
 */
const PLAYGROUND_TIMEOUT_MS = 120_000;

export function PlaygroundPage() {
  const session = useSessionContext();
  const [model, setModel] = useState("");
  const [prompt, setPrompt] = useState("");
  /** 图片附件：data URL（base64），随请求以 `image_url` 块发送，浏览器本地预览。 */
  const [images, setImages] = useState<{ id: string; url: string }[]>([]);
  const fileInput = useRef<HTMLInputElement>(null);
  const [stream, setStream] = useState(true);
  const [temperature, setTemperature] = useState("");
  const [maxTokens, setMaxTokens] = useState("");
  const [effort, setEffort] = useState("");
  const [answer, setAnswer] = useState("");
  const [reasoning, setReasoning] = useState("");
  const [usage, setUsage] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const modelsQuery = usePlaygroundModels();
  const fetched = modelsQuery.data?.data ?? [];

  // 每个模型的受控值：单渠道模型带 @provider（该组选项即强制指定），
  // 多渠道模型用裸 id（走自动路由）。
  const models = fetched.map((item) => ({ ...item, value: modelValue(item) }));
  const selectedModel = model || pickDefaultModel(models);
  // 选中模型的元数据：优先精确匹配 value，退化为按小写基础名匹配
  const selectedInfo =
    models.find((item) => item.value === selectedModel) ??
    models.find((item) => item.value === selectedModel.split("@")[0]);
  // 该模型可选的推理档位候选；null = 不适用，控件不渲染
  const efforts = effortOptions(selectedInfo);
  // 换模型时清空推理档位：档位是模型相关参数，沿用上一个模型的选择
  // 既可能落在新模型的枚举之外，也会悄悄改变请求行为。值没变时不动，
  // 避免重复点同一行把用户刚选的档位抹掉。
  const pickModel = (value: string) => {
    if (value !== selectedModel) setEffort("");
    setModel(value);
  };

  // 图片附件上限：单张 4MB、最多 4 张。请求体上限 16MB（webapp/limits），
  // base64 约膨胀 4/3，4 张 4MB 恰好留出提示词与 JSON 的余量。
  const MAX_IMAGE_BYTES = 4 * 1024 * 1024;
  const MAX_IMAGES = 4;

  const addImages = (files: File[]) => {
    const picked = files.filter((file) => file.type.startsWith("image/"));
    if (picked.length === 0) return;
    if (images.length + picked.length > MAX_IMAGES) {
      setError(`最多 ${MAX_IMAGES} 张图片`);
      return;
    }
    for (const file of picked) {
      if (file.size > MAX_IMAGE_BYTES) {
        setError(`「${file.name}」超过 4MB，请压缩后再试`);
        return;
      }
      const reader = new FileReader();
      reader.onload = () => {
        const url = typeof reader.result === "string" ? reader.result : "";
        if (url) {
          setImages((prev) =>
            prev.length >= MAX_IMAGES ? prev : [...prev, { id: crypto.randomUUID(), url }]);
        }
      };
      reader.onerror = () => setError(`「${file.name}」读取失败`);
      reader.readAsDataURL(file);
    }
  };

  /** 有图时把提示词与附件拼成 content 块数组（OpenAI 多模态形状）。 */
  const buildContent = () => {
    if (images.length === 0) return prompt;
    const blocks: { type: string; text?: string; image_url?: { url: string } }[] = [];
    if (prompt.trim()) blocks.push({ type: "text", text: prompt });
    for (const image of images) {
      blocks.push({ type: "image_url", image_url: { url: image.url } });
    }
    return blocks;
  };

  const send = async (event: React.FormEvent) => {
    event.preventDefault();
    // 快捷键（⌘/Ctrl+Enter）走 requestSubmit()，不受发送按钮 disabled 约束；
    // 请求进行中直接忽略，避免重复 POST 重复计费。
    if (busy) return;
    // 推理档位随模型能力变化：当前模型不适用（控件不渲染）时，即使 state 里
    // 还留着上一个模型选的档位也不发送，避免把无关参数塞给不支持的模型。
    const built = buildSamplingParams({
      temperature,
      maxTokens,
      effort: effortOptions(selectedInfo) ? effort : "",
    });
    if (built.error) {
      setError(built.error);
      return;
    }
    setBusy(true);
    setError(null);
    setAnswer("");
    setReasoning("");
    setUsage(null);
    // 只给「连上 + 首字节」设超时：定时器在响应头到达时清掉，读流阶段不受约束
    // （否则长推理的流式回答会被 120s 硬砍）。后端挂死时 abort 让 finally 复位 busy。
    const controller = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, PLAYGROUND_TIMEOUT_MS);
    // 首字节之后不再受超时约束：读流用 response 自己的连接与 signal 无关
    const stopTimer = () => clearTimeout(timer);
    try {
      const response = await fetch("/api/playground/chat/completions", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          // CSRF 纵深：写请求带自定义头（与 api/client.ts 一致）
          "X-Requested-With": "XMLHttpRequest",
        },
        credentials: "same-origin",
        signal: controller.signal,
        body: JSON.stringify({
          model: selectedModel,
          messages: [{ role: "user", content: buildContent() }],
          stream,
          ...built.params,
        }),
      });
      stopTimer();
      if (response.status === 401) {
        // 会话过期：走统一出口（清缓存 + 回登录页），不要裸跳转绕开它
        notifyUnauthorized();
        setError("会话已过期，请重新登录。");
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
      // 后端 4xx/5xx 已在上面解析过 error.message（受控文案）；走到这里的是
      // 网络层异常（fetch 抛 TypeError 等），其 message 是英文实现细节，
      // 不该直接渲染给用户。
      setError(timedOut
        ? `请求超时（${PLAYGROUND_TIMEOUT_MS / 1000} 秒未收到响应），请稍后重试。`
        : errorText(caught, "请求失败，请稍后重试。"));
    } finally {
      stopTimer();
      setBusy(false);
    }
  };

  return (
    <div className="space-y-6" data-testid="playground-page">
      <PageHeader
        eyebrow="控制台"
        title="Playground"
        description="用登录会话直接测试模型调度，无需 API Key；请求会走与外部 API 相同的调度与统计，用量计入当前用户。"
        icon={<TerminalSquare className="size-5" />}
      />

      {(error || modelsQuery.error) && (
        <div data-testid="playground-error">
          <Notice tone="danger">
            {error ?? "模型列表加载失败，请刷新重试"}
          </Notice>
        </div>
      )}

      <Panel title="模型">
        <div className="space-y-3">
          <ModelPicker
            models={models}
            value={selectedModel}
            onChange={pickModel}
            onPin={pickModel}
            loading={modelsQuery.isFetching && fetched.length === 0}
          />
          <p className="text-xs leading-relaxed text-muted-foreground">
            默认由调度器在可用渠道间自动选健康号；在「当前选择」条点渠道可强制走单一渠道（如 glm-5.2@trae）。
          </p>

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

          {/* 能力分（Artificial Analysis 指数，经 OpenRouter 公开接口）：与上面的
              上游元数据分开一张卡——它是第三方成绩，不是本服务实测，混在一起会被
              误读成本服务的字段。匹配不到该模型时整卡不渲染。 */}
          {selectedInfo?.benchmarks && (
            <Card
              size="sm"
              className="flex flex-row flex-wrap items-center gap-x-4 gap-y-1 bg-muted/40 px-3 py-2 text-xs text-muted-foreground ring-border"
              data-testid="playground-benchmarks"
            >
              <span className="font-medium text-foreground">能力分</span>
              {selectedInfo.benchmarks.intelligence_index !== undefined && (
                <span>
                  智能 <span className="font-medium tabular-nums text-foreground">
                    {selectedInfo.benchmarks.intelligence_index}
                  </span>
                </span>
              )}
              {selectedInfo.benchmarks.coding_index !== undefined && (
                <span>
                  编程 <span className="font-medium tabular-nums text-foreground">
                    {selectedInfo.benchmarks.coding_index}
                  </span>
                </span>
              )}
              {selectedInfo.benchmarks.agentic_index !== undefined && (
                <span>
                  智能体 <span className="font-medium tabular-nums text-foreground">
                    {selectedInfo.benchmarks.agentic_index}
                  </span>
                </span>
              )}
              <span className="text-[11px]">
                Artificial Analysis 指数，经 OpenRouter 公开接口；非本服务实测
              </span>
            </Card>
          )}
        </div>
      </Panel>

      {/* 宽屏把「请求」与「响应」并排：发送按钮与回答同屏，不必先滚过整个模型列表 */}
      <div className="grid gap-6 lg:grid-cols-2 lg:items-start">
        <Panel title="请求">
          <form onSubmit={send} className="space-y-3">
            <Field label="提示词" htmlFor="playground-prompt-input">
              <Textarea
                id="playground-prompt-input"
                rows={6}
                value={prompt}
                data-testid="playground-prompt"
                placeholder="输入提示词，Ctrl/⌘ + Enter 发送"
                className="font-mono text-xs"
                onKeyDown={(event) => {
                  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                    event.preventDefault();
                    event.currentTarget.form?.requestSubmit();
                  }
                }}
                onChange={(event) => setPrompt(event.target.value)}
                onPaste={(event) => {
                  const files = Array.from(event.clipboardData.files ?? []);
                  if (files.length > 0) {
                    event.preventDefault();
                    addImages(files);
                  }
                }}
              />
            </Field>

            {/* 图片附件：后端三出口已全链支持 vision，这里只补齐入口 */}
            <div className="space-y-2" data-testid="playground-images">
              <div className="flex flex-wrap items-center gap-2">
                {images.map((image) => (
                  <div
                    key={image.id}
                    data-testid="playground-image-preview"
                    className="group relative size-16 overflow-hidden rounded-md border border-border"
                  >
                    <img src={image.url} alt="附件预览" className="size-full object-cover" />
                    <button
                      type="button"
                      data-testid="playground-remove-image"
                      aria-label="移除图片"
                      className="absolute right-0.5 top-0.5 rounded bg-background/80 p-0.5 text-muted-foreground hover:text-destructive"
                      onClick={() =>
                        setImages((prev) => prev.filter((item) => item.id !== image.id))}
                    >
                      <X className="size-3" />
                    </button>
                  </div>
                ))}
                {images.length < MAX_IMAGES && (
                  <Button
                    type="button"
                    variant="default"
                    size="sm"
                    className="gap-1.5"
                    data-testid="playground-add-image"
                    onClick={() => fileInput.current?.click()}
                  >
                    <ImagePlus className="size-4" />
                    图片
                  </Button>
                )}
                <input
                  ref={fileInput}
                  type="file"
                  accept="image/*"
                  multiple
                  hidden
                  data-testid="playground-image-input"
                  onChange={(event) => {
                    addImages(Array.from(event.target.files ?? []));
                    event.target.value = "";
                  }}
                />
              </div>
              <p className="text-xs text-muted-foreground">
                支持粘贴或选择图片（单张 ≤ 4MB，最多 {MAX_IMAGES} 张）；模型不支持图片时上游会拒绝。
              </p>
            </div>
            <Label className="w-fit cursor-pointer gap-2 text-sm font-normal">
              <Checkbox
                checked={stream}
                data-testid="stream-toggle"
                onCheckedChange={(value) => setStream(value === true)}
              />
              流式输出
            </Label>

            {/* 高级参数：留空即不发送该键，由渠道走各自的默认值 */}
            <div className="grid gap-3 sm:grid-cols-3" data-testid="playground-advanced">
              <Field label="temperature" htmlFor="param-temperature-input" hint="留空不发送；0–2 是 OpenAI 惯例，本服务只做有限值透传">
                <Input
                  id="param-temperature-input"
                  type="number"
                  step="0.1"
                  min="0"
                  value={temperature}
                  placeholder="渠道默认"
                  data-testid="param-temperature"
                  onChange={(event) => setTemperature(event.target.value)}
                />
              </Field>
              <Field
                label="max_tokens"
                htmlFor="param-max-tokens-input"
                hint={
                  selectedInfo?.max_output_tokens !== undefined
                    ? `留空不发送；该模型上限 ${selectedInfo.max_output_tokens.toLocaleString()}`
                    : "留空不发送"
                }
              >
                <Input
                  id="param-max-tokens-input"
                  type="number"
                  step="1"
                  min="1"
                  value={maxTokens}
                  placeholder="渠道默认"
                  data-testid="param-max-tokens"
                  onChange={(event) => setMaxTokens(event.target.value)}
                />
              </Field>
              {efforts && (
                <Field
                  label="reasoning_effort"
                  htmlFor="param-effort-select"
                  hint={
                    selectedInfo?.default_effort
                      ? `该模型默认档位 ${selectedInfo.default_effort}；留空不发送，渠道可能自补默认`
                      : "留空不发送；档位由上游裁决，不支持的值会返回上游错误"
                  }
                >
                  <Select
                    id="param-effort-select"
                    value={effort}
                    data-testid="param-effort"
                    onChange={(event) => setEffort(event.target.value)}
                  >
                    <option value="">不发送</option>
                    {efforts.map((level) => (
                      <option key={level} value={level}>{level}</option>
                    ))}
                  </Select>
                </Field>
              )}
            </div>

            <Button type="submit" variant="primary" disabled={busy} data-testid="send-request" className="gap-1.5">
              {busy ? <Loader2 className="size-4 animate-spin" /> : <Send className="size-4" />}
              {busy ? "请求中…" : "发送"}
            </Button>
          </form>
        </Panel>

        {/* 请求/流式响应无焦点变化：aria-live 让读屏可感知回答到达 */}
        <div aria-live="polite">
          <Panel title="响应">
            {answer || reasoning ? (
              <>
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
              </>
            ) : (
              <Empty>
                <div className="space-y-1">
                  <p>选择模型并填写提示词后发送，结果会显示在这里。</p>
                  <p className="text-xs">
                    用量计入 {session.username}，与外部 API 同一条调度与统计链路。
                    API Key 仅供外部客户端接入使用。
                  </p>
                </div>
              </Empty>
            )}
          </Panel>
        </div>
      </div>
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