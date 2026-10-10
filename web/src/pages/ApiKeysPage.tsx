import { Check, ChevronDown, Copy, KeyRound, Plus, Trash2, X } from "lucide-react";
import { useState } from "react";
import { api } from "../api/client";
import { useApiKeys, useQueryClient } from "../api/hooks";
import { formatTime } from "../api/display";
import { cn } from "../lib/utils";
import { useDialogFocus } from "../hooks/useDialogFocus";
import { useSort } from "../hooks/useSort";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { SortableHead } from "../components/SortableHead";
import { PROVIDER_LABEL, PROVIDER_ORDER } from "../api/providers";
import type { ApiKeyCreated } from "../api/types";
import {
  Badge,
  Button,
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
  EmptyState,
  Field,
  Input,
  Notice,
  Panel,
  Select,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../ui";

/** 渠道绑定的下拉选项：值与后端 KNOWN_PROVIDERS 对齐，空串 = 自动。 */
export const PROVIDER_BINDING_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "自动（不限定渠道）" },
  // 渠道集合与展示名都来自 providers.ts 的单一来源，新增渠道不再改这里
  ...PROVIDER_ORDER.map((provider) => ({ value: provider, label: PROVIDER_LABEL[provider] })),
];

/** 列表里展示绑定渠道：空串 → 「自动」 */
export function providerBindingLabel(binding: string | null | undefined): string {
  if (!binding) return "自动";
  return PROVIDER_BINDING_OPTIONS.find((o) => o.value === binding)?.label ?? binding;
}

/** `datetime-local` 值（本地时区的 `YYYY-MM-DDTHH:mm`）→ epoch 秒；空串 = 永不过期。 */
export function parseExpiresAt(value: string): number | null {
  const raw = value.trim();
  if (!raw) return null;
  const ms = new Date(raw).getTime();
  if (!Number.isFinite(ms)) throw new Error("invalid expires_at");
  return Math.floor(ms / 1000);
}

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

function CopyButton({
  label,
  onCopy,
  testid,
}: {
  label: string;
  onCopy: () => void;
  testid?: string;
}) {
  return (
    <Button
      size="sm"
      variant="ghost"
      data-testid={testid}
      onClick={onCopy}
      className="gap-1"
    >
      {label === "已复制" ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
      {label}
    </Button>
  );
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

/** 客户端接入面板的公共骨架：折叠交互与「模型与渠道调度说明」(HelpBlock) 一致，
 *  卡片 header 的「说明 / 收起」按钮切换内容，不引入动画。 */
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
function OpenAIEntry({ apiKey }: { apiKey: string }) {
  const baseUrl = openaiBaseUrl();
  const examples = openaiExamples(baseUrl, apiKey);
  const [copied, setCopied] = useState<string | null>(null);

  const copy = async (label: string, text: string) => {
    await navigator.clipboard.writeText(text);
    setCopied(label);
  };

  return (
    <ClientEntryPanel title="OpenAI 客户端接入" testid="openai-entry">
      <BaseUrlRow
        baseUrl={baseUrl}
        copied={copied === "url"}
        onCopy={() => void copy("url", baseUrl)}
        testid="openai-base-url"
      />
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
function AnthropicEntry({ apiKey }: { apiKey: string }) {
  const baseUrl = anthropicBaseUrl();
  const examples = anthropicExamples(baseUrl, apiKey);
  const [copied, setCopied] = useState<string | null>(null);

  const copy = async (label: string, text: string) => {
    await navigator.clipboard.writeText(text);
    setCopied(label);
  };

  return (
    <ClientEntryPanel title="Anthropic 客户端接入" testid="anthropic-entry">
      <BaseUrlRow
        baseUrl={baseUrl}
        copied={copied === "url"}
        onCopy={() => void copy("url", baseUrl)}
        testid="anthropic-base-url"
      />
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

export function ApiKeysPage() {
  const { sort, order, toggle } = useSort("created_at", "asc", { last_used_at: "desc" });
  const { data, isLoading } = useApiKeys(undefined, sort, order);
  const client = useQueryClient();
  const [name, setName] = useState("");
  const [binding, setBinding] = useState("");
  const [allowedIps, setAllowedIps] = useState("");
  const [allowedModels, setAllowedModels] = useState("");
  const [expiresAt, setExpiresAt] = useState("");
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  // 创建 Key 收进对话框（低频动作不常驻占屏），由「我的 API Key」右上角触发
  const [createOpen, setCreateOpen] = useState(false);

  const keys = data?.api_keys ?? [];

  const create = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    try {
      const result = await api.createApiKey({
        name,
        provider_binding: binding,
        allowed_ips: allowedIps.trim(),
        allowed_models: allowedModels.trim(),
        expires_at: parseExpiresAt(expiresAt),
      });
      setCreated(result);
      setName("");
      setBinding("");
      setAllowedIps("");
      setAllowedModels("");
      setExpiresAt("");
      setCopied(false);
      await client.invalidateQueries({ queryKey: ["admin"] });
    } catch {
      setError("创建失败：请检查渠道绑定、模型白名单、IP 白名单与到期时间格式");
    }
  };

  const remove = async (id: string) => {
    setError(null);
    try {
      await api.deleteApiKey(id);
      setConfirming(null);
      await client.invalidateQueries({ queryKey: ["admin"] });
    } catch {
      setError("删除失败");
    }
  };

  const copy = async () => {
    if (!created) return;
    await navigator.clipboard.writeText(created.api_key);
    setCopied(true);
  };

  const closeCreate = () => {
    setCreateOpen(false);
    setError(null);
    // 明文 Key 只应存在于创建确认弹窗的生命周期内（L3）：关闭即从 state 抹掉，
    // 否则它会继续留在 OpenAI/Anthropic 接入面板与页面内存里，与「只显示一次」不符。
    setCreated(null);
    setCopied(false);
  };

  const createDialogRef = useDialogFocus<HTMLDivElement>(closeCreate, createOpen);

  return (
    <div className="space-y-6" data-testid="api-keys-page">
      <PageHeader
        eyebrow="控制台"
        title="API Key 管理"
        description="创建外部客户端（ChatGPT Next Web、LobeChat、Cursor 等）接入用的 Key，协议与 OpenAI 兼容。Key 仅在创建时完整显示一次，请立即保存；用量按 Key 归属用户统计。"
        icon={<KeyRound className="size-5" />}
      />
      <OpenAIEntry apiKey={created?.api_key ?? ""} />
      <AnthropicEntry apiKey={created?.api_key ?? ""} />

      <Panel
        title="我的 API Key"
        action={
          <Button
            size="sm"
            variant="primary"
            data-testid="open-create-key-dialog"
            onClick={() => setCreateOpen(true)}
          >
            <Plus className="size-4" /> 创建 API Key
          </Button>
        }
      >
        {isLoading ? (
          <PageSkeleton variant="table" rows={4} />
        ) : keys.length === 0 ? (
          <EmptyState
            icon={<KeyRound className="size-5" />}
            title="还没有 API Key"
            description="点右上角「创建 API Key」生成一个，复制到外部客户端即可接入。"
            data-testid="no-keys"
          />
        ) : (
          <Table data-testid="keys-table">
            <TableHeader>
              <TableRow>
                <SortableHead label="名称" columnKey="name" active={sort === "name"}
                              direction={order} onToggle={toggle} testId="sort-name" />
                <TableHead>Key</TableHead>
                <SortableHead label="渠道" columnKey="provider_binding"
                              active={sort === "provider_binding"} direction={order}
                              onToggle={toggle} testId="sort-provider_binding" />
                <TableHead>来源 IP</TableHead>
                <TableHead>模型白名单</TableHead>
                <SortableHead label="到期" columnKey="expires_at" active={sort === "expires_at"}
                              direction={order} onToggle={toggle} testId="sort-expires_at" />
                <SortableHead label="创建时间" columnKey="created_at" active={sort === "created_at"}
                              direction={order} onToggle={toggle} testId="sort-created_at" />
                <SortableHead label="最后使用" columnKey="last_used_at"
                              active={sort === "last_used_at"} direction={order}
                              onToggle={toggle} testId="sort-last_used_at" />
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {keys.map((key) => (
                <TableRow key={key.id}>
                  <TableCell>{key.name || "—"}</TableCell>
                  <TableCell className="font-mono text-xs">{key.preview}</TableCell>
                  <TableCell className="text-xs" data-testid={`key-binding-${key.id}`}>
                    {providerBindingLabel(key.provider_binding)}
                  </TableCell>
                  <TableCell className="font-mono text-xs" data-testid={`key-ips-${key.id}`}>
                    {key.allowed_ips || "不限制"}
                  </TableCell>
                  <TableCell className="font-mono text-xs" data-testid={`key-models-${key.id}`}>
                    {key.allowed_models || "不限制"}
                  </TableCell>
                  <TableCell className="text-xs" data-testid={`key-expires-${key.id}`}>
                    {key.expires_at ? formatTime(key.expires_at) : "永不过期"}
                  </TableCell>
                  <TableCell className="text-xs">{formatTime(key.created_at)}</TableCell>
                  <TableCell className="text-xs">
                    {key.last_used_at ? formatTime(key.last_used_at) : "从未使用"}
                  </TableCell>
                  <TableCell className="text-right">
                    {confirming === key.id ? (
                      <span className="inline-flex gap-2">
                        <Button size="sm" variant="danger" onClick={() => remove(key.id)}>
                          确认删除
                        </Button>
                        <Button size="sm" variant="ghost" onClick={() => setConfirming(null)}>
                          取消
                        </Button>
                      </span>
                    ) : (
                      <Button size="sm" variant="ghost" onClick={() => setConfirming(key.id)}>
                        <Trash2 className="mr-1 size-3.5" />
                        删除
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Panel>

      {/* 创建 Key 对话框：创建成功后仍停留在对话框内展示一次性明文，由用户主动关闭 */}
      {createOpen && (
        <div
          className="fixed inset-0 z-50 grid place-items-center bg-black/50 p-4 backdrop-blur-sm"
          onClick={closeCreate}
        >
          <div
            ref={createDialogRef}
            role="dialog"
            aria-modal="true"
            aria-label="创建 API Key"
            data-testid="create-key-dialog"
            onClick={(event) => event.stopPropagation()}
            className="max-h-[90vh] w-full max-w-lg animate-in overflow-y-auto rounded-xl border border-border bg-card p-5 shadow-xl fade-in-0 zoom-in-95"
          >
            <div className="mb-4 flex items-center justify-between">
              <h2 className="flex items-center gap-2 text-sm font-semibold">
                <Plus className="size-4 text-muted-foreground" />
                创建 API Key
              </h2>
              <Button
                size="icon"
                variant="ghost"
                aria-label="关闭"
                data-testid="create-key-close"
                onClick={closeCreate}
              >
                <X className="size-4" />
              </Button>
            </div>

            {created ? (
              <div aria-live="polite">
                <Notice tone="warn">此 Key 只会显示这一次，请立即复制保存；关闭后无法再次查看。</Notice>
                <div className="mt-3 flex items-center gap-3">
                  <code
                    data-testid="new-key-plaintext"
                    className="flex-1 break-all rounded-lg border border-input bg-muted/40 px-3 py-2 font-mono text-xs"
                  >
                    {created.api_key}
                  </code>
                  <CopyButton label={copied ? "已复制" : "复制"} onCopy={copy} />
                  <Button size="sm" variant="ghost" onClick={closeCreate}>
                    我已保存
                  </Button>
                </div>
              </div>
            ) : (
              <form onSubmit={create} className="space-y-3">
                <Field label="名称（可选）">
                  <Input
                    value={name}
                    data-testid="key-name"
                    placeholder="例如 laptop"
                    onChange={(event) => setName(event.target.value)}
                  />
                </Field>
                <Field label="渠道绑定">
                  <Select
                    value={binding}
                    data-testid="key-binding"
                    onChange={(event) => setBinding(event.target.value)}
                  >
                    {PROVIDER_BINDING_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </Select>
                </Field>
                <Field label="来源 IP 白名单（逗号分隔 IP/CIDR，留空不限制）">
                  <Input
                    value={allowedIps}
                    data-testid="key-allowed-ips"
                    placeholder="例如 203.0.113.9,10.0.0.0/8"
                    onChange={(event) => setAllowedIps(event.target.value)}
                  />
                </Field>
                <Field label="模型白名单（逗号分隔模型名或 glob，留空不限制）">
                  <Input
                    value={allowedModels}
                    data-testid="key-allowed-models"
                    placeholder="例如 glm-*,kimi-k3"
                    onChange={(event) => setAllowedModels(event.target.value)}
                  />
                </Field>
                <Field label="到期时间（留空永不过期）">
                  <Input
                    type="datetime-local"
                    value={expiresAt}
                    data-testid="key-expires-at"
                    onChange={(event) => setExpiresAt(event.target.value)}
                  />
                </Field>
                {/* 创建结果（成功/失败）无焦点变化，aria-live 让读屏可感知 */}
                <div aria-live="polite">
                  {error && (
                    <div role="alert">
                      <Notice tone="danger">{error}</Notice>
                    </div>
                  )}
                </div>
                <div className="flex justify-end gap-2">
                  <Button type="button" variant="ghost" onClick={closeCreate}>
                    取消
                  </Button>
                  <Button type="submit" variant="primary">
                    <Plus className="mr-1 size-4" />
                    创建
                  </Button>
                </div>
              </form>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
