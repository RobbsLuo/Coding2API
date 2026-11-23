import { ChevronDown, Plus } from "lucide-react";
import { useState } from "react";
import { copyText } from "../../lib/clipboard";
import { cn } from "../../lib/utils";
import {
  Badge,
  Button,
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
  Notice,
  Panel,
} from "../../ui";
import { CopyButton } from "./shared";

/** OpenAI 兼容入口的 Base URL：本服务地址 + /v1 */
export const openaiBaseUrl = (): string => `${window.location.origin}/v1`;

/** 接入示例代码；apiKey 占位时用 sk-… */
export function openaiExamples(baseUrl: string, apiKey: string): {
  curl: string;
  python: string;
  balance: string;
  responses: string;
} {
  const key = apiKey || "sk-…";
  return {
    curl: `curl ${baseUrl}/chat/completions \\
  -H "Authorization: Bearer ${key}" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "glm-5.2",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": true
  }'`,
    python: `from openai import OpenAI

client = OpenAI(base_url="${baseUrl}", api_key="${key}")
resp = client.chat.completions.create(
    model="glm-5.2",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)`,
    balance: `curl ${baseUrl}/user/balance \\
  -H "Authorization: Bearer ${key}"`,
    // Codex CLI 用 env_key 引用环境变量名（不是 Key 本身），故先 export 再引用
    responses: `export CODING2API_KEY=${key}
codex -c "model_providers.coding2api={ name='coding2api', base_url='${baseUrl}', wire_api='responses', env_key='CODING2API_KEY' }" \\
      -c model_provider=coding2api \\
      -c model='glm-5.2' \\
      '你的任务'`,
  };
}

/** Anthropic 客户端接入的 Base URL：SDK 自己拼 /v1/messages，填到根 */
export const anthropicBaseUrl = (): string => window.location.origin;

/** Claude Code 接入示例；ANTHROPIC_AUTH_TOKEN 走 Bearer，ANTHROPIC_API_KEY 走 x-api-key */
export function anthropicExamples(baseUrl: string, apiKey: string): { messages: string } {
  const key = apiKey || "sk-…";
  return {
    messages: `export ANTHROPIC_BASE_URL=${baseUrl}
export ANTHROPIC_AUTH_TOKEN=${key}
claude`,
  };
}

/** 端点展开后的单条调用示例：标题 + 复制按钮 + 代码块 */
function CodeExample({
  label,
  text,
  copied,
  onCopy,
  testid,
  copyTestid,
}: {
  label: string;
  text: string;
  copied: boolean;
  onCopy: () => void;
  testid: string;
  copyTestid?: string;
}) {
  return (
    <div>
      <div className="mb-1 flex items-center justify-between text-foreground">
        <span>{label}</span>
        <CopyButton label={copied ? "已复制" : "复制"} onCopy={onCopy} testid={copyTestid} />
      </div>
      <pre data-testid={testid} className="overflow-x-auto rounded-lg border border-input bg-muted/40 p-3 font-mono text-xs">
        {text}
      </pre>
    </div>
  );
}

/** 折叠的端点行：trigger 显示方法与说明，展开后可复制调用示例 */
function EndpointRow({
  method,
  path,
  description,
  children,
  testid,
}: {
  method: string;
  path: string;
  description: string;
  children: React.ReactNode;
  testid: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <Collapsible
      open={open}
      onOpenChange={setOpen}
      data-testid={testid}
      className="group rounded-lg border border-border px-3 py-2"
    >
      <CollapsibleTrigger
        data-testid={`${testid}-trigger`}
        aria-expanded={open}
        className="flex w-full cursor-pointer select-none flex-wrap items-center gap-2"
      >
        <Badge>{method}</Badge> <code>{path}</code>
        <span>{description}</span>
        <span className="ml-auto inline-flex items-center gap-1 text-muted-foreground group-data-[state=open]:hidden">
          <Plus className="size-3" />调用示例
        </span>
      </CollapsibleTrigger>
      <CollapsibleContent forceMount className="mt-3 space-y-3 data-[state=closed]:hidden">
        {children}
      </CollapsibleContent>
    </Collapsible>
  );
}

/** 客户端接入面板的公共骨架：卡片 header 的「说明 / 收起」按钮切换内容，
 *  默认收起，不引入动画。 */
function ClientEntryPanel({
  title,
  testid,
  children,
}: {
  title: string;
  testid: string;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <Panel
      title={title}
      action={
        <Button
          size="sm"
          variant="ghost"
          data-testid={`${testid}-toggle`}
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
        >
          <ChevronDown className={cn("size-4 transition-transform", open && "rotate-180")} />
          {open ? "收起" : "说明"}
        </Button>
      }
    >
      {open ? (
        <div className="space-y-4 text-sm" data-testid={testid}>
          {children}
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">
          展开查看 Base URL、端点列表与可复制的接入示例。
        </p>
      )}
    </Panel>
  );
}

/** Base URL 行：代码框 + 复制按钮 */
function BaseUrlRow({
  baseUrl,
  copied,
  onCopy,
  testid,
}: {
  baseUrl: string;
  copied: boolean;
  onCopy: () => void;
  testid: string;
}) {
  return (
    <div>
      <div className="mb-1 text-xs text-muted-foreground">Base URL</div>
      <div className="flex items-center gap-2">
        <code
          data-testid={testid}
          className="flex-1 truncate rounded-lg border border-input bg-muted/40 px-3 py-2 font-mono text-xs transition-colors hover:border-ring"
        >
          {baseUrl}
        </code>
        <CopyButton label={copied ? "已复制" : "复制"} onCopy={onCopy} />
      </div>
    </div>
  );
}

/** OpenAI 客户端接入面板：Base URL + 端点 + 可复制的示例。 */
export function OpenAIEntry({ apiKey }: { apiKey: string }) {
  const baseUrl = openaiBaseUrl();
  const examples = openaiExamples(baseUrl, apiKey);
  const [copied, setCopied] = useState<string | null>(null);
  const [copyFailed, setCopyFailed] = useState(false);

  const copy = async (label: string, text: string) => {
    const ok = await copyText(text);
    setCopied(ok ? label : null);
    // 局域网 http 部署没有 Clipboard API：明确提示，别让用户以为已复制
    setCopyFailed(!ok);
  };

  return (
    <ClientEntryPanel title="OpenAI 客户端接入" testid="openai-entry">
      <BaseUrlRow
        baseUrl={baseUrl}
        copied={copied === "url"}
        onCopy={() => void copy("url", baseUrl)}
        testid="openai-base-url"
      />
      {copyFailed && (
        <p className="mt-2 text-xs text-danger" data-testid="openai-copy-failed">
          浏览器不允许自动复制，请手动选中内容复制。
        </p>
      )}
      <ul className="space-y-1 text-xs text-muted-foreground">
        <li>
          {/* 示例默认折叠：点开端点行查看 curl / SDK 调用方式 */}
          <EndpointRow method="POST" path={`${baseUrl}/chat/completions`} description="对话补全（流式 / 非流式）" testid="example-details">
            <CodeExample label="curl 示例" text={examples.curl} copied={copied === "curl"} onCopy={() => void copy("curl", examples.curl)} testid="example-curl" copyTestid="copy-curl" />
            <CodeExample label="OpenAI Python SDK" text={examples.python} copied={copied === "python"} onCopy={() => void copy("python", examples.python)} testid="example-python" copyTestid="copy-python" />
          </EndpointRow>
        </li>
        <li>
          <EndpointRow method="GET" path={`${baseUrl}/user/balance`} description="余额查询（上游额度，DeepSeek 兼容结构）" testid="example-details-balance">
            <CodeExample label="curl 示例" text={examples.balance} copied={copied === "balance"} onCopy={() => void copy("balance", examples.balance)} testid="example-balance-curl" copyTestid="copy-balance-curl" />
          </EndpointRow>
        </li>
        <li>
          <EndpointRow method="POST" path={`${baseUrl}/responses`} description="Responses 子集（供 Codex CLI 接入）" testid="example-details-responses">
            <CodeExample label="Codex CLI 配置" text={examples.responses} copied={copied === "responses"} onCopy={() => void copy("responses", examples.responses)} testid="example-responses" copyTestid="copy-responses" />
          </EndpointRow>
        </li>
        <li>
          <Badge>GET</Badge> <code>{baseUrl}/models</code>　模型列表
        </li>
      </ul>
      <Notice tone="muted">
        任何兼容 OpenAI 协议的客户端（ChatGPT Next Web、LobeChat、Cursor 等）
        都可以按上面的 Base URL + API Key 接入。模型名从 /v1/models 获取。
      </Notice>
      <Notice tone="muted">
        {/* 整段包一层 span：ShadAlert 是 grid 容器，直接放多个行内元素会被拆成多行 */}
        <span>
          <code>POST /v1/responses</code> 只实现 Codex CLI 用到的子集，与{" "}
          <code>/v1/chat/completions</code> 共用同一套选号、冷却、轮换与会话粘性。
          <code>store=true</code>、<code>previous_response_id</code> 与{" "}
          <code>web_search</code> 等服务端私有工具会被显式拒绝（400），不静默降级。
        </span>
      </Notice>
    </ClientEntryPanel>
  );
}

/** Anthropic 客户端接入面板：Claude Code 等只走 Anthropic 协议的客户端从这里抄配置。 */
export function AnthropicEntry({ apiKey }: { apiKey: string }) {
  const baseUrl = anthropicBaseUrl();
  const examples = anthropicExamples(baseUrl, apiKey);
  const [copied, setCopied] = useState<string | null>(null);
  const [copyFailed, setCopyFailed] = useState(false);

  const copy = async (label: string, text: string) => {
    const ok = await copyText(text);
    setCopied(ok ? label : null);
    // 局域网 http 部署没有 Clipboard API：明确提示，别让用户以为已复制
    setCopyFailed(!ok);
  };

  return (
    <ClientEntryPanel title="Anthropic 客户端接入" testid="anthropic-entry">
      <BaseUrlRow
        baseUrl={baseUrl}
        copied={copied === "url"}
        onCopy={() => void copy("url", baseUrl)}
        testid="anthropic-base-url"
      />
      {copyFailed && (
        <p className="mt-2 text-xs text-danger" data-testid="anthropic-copy-failed">
          浏览器不允许自动复制，请手动选中内容复制。
        </p>
      )}
      <ul className="space-y-1 text-xs text-muted-foreground">
        <li>
          <EndpointRow method="POST" path={`${baseUrl}/v1/messages`} description="对话补全（流式 / 非流式，SDK 自动拼接路径）" testid="example-details-messages">
            <CodeExample label="Claude Code 配置" text={examples.messages} copied={copied === "messages"} onCopy={() => void copy("messages", examples.messages)} testid="example-messages" copyTestid="copy-messages" />
          </EndpointRow>
        </li>
        <li>
          <Badge>POST</Badge> <code>{baseUrl}/v1/messages/count_tokens</code>　输入 token 估算
        </li>
      </ul>
      <Notice tone="muted">
        Claude Code 等 Anthropic 协议客户端按上面的 Base URL + API Key 接入；
        与 OpenAI 出口共用同一套 Key、选号、冷却、轮换与会话粘性。
      </Notice>
      <Notice tone="muted">
        {/* 整段包一层 span：ShadAlert 是 grid 容器，直接放多个行内元素会被拆成多行 */}
        <span>
          鉴权同时接受 <code>x-api-key</code>（<code>ANTHROPIC_API_KEY</code>）与{" "}
          <code>Authorization: Bearer</code>（<code>ANTHROPIC_AUTH_TOKEN</code>）。
          只实现 Anthropic Messages 子集：图片 / 文档块与 Anthropic 服务端工具
          （<code>web_search</code> / <code>computer</code> 等）一律显式 400，不静默降级。
        </span>
      </Notice>
    </ClientEntryPanel>
  );
}
