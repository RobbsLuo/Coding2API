import { Check, ChevronDown, Copy, KeyRound, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { api } from "../api/client";
import { useApiKeys, useQueryClient } from "../api/hooks";
import { formatTime } from "../api/display";
import { cn } from "../lib/utils";
import { PageHeader } from "../components/PageHeader";
import type { ApiKeyCreated } from "../api/types";
import {
  Badge,
  Button,
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
  Empty,
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
export const PROVIDER_BINDING_OPTIONS = [
  { value: "", label: "自动（不限定渠道）" },
  { value: "codebuddy", label: "CodeBuddy" },
  { value: "trae", label: "TRAE" },
];

/** 列表里展示绑定渠道：空串 → 「自动」 */
export function providerBindingLabel(binding: string | null | undefined): string {
  if (!binding) return "自动";
  return PROVIDER_BINDING_OPTIONS.find((o) => o.value === binding)?.label ?? binding;
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

/** OpenAI 客户端接入面板：Base URL + 端点 + 可复制的示例。
 *  折叠交互与「模型与渠道调度说明」(HelpBlock) 一致：卡片 header 的「说明 / 收起」
 *  按钮切换内容，不引入动画。 */
function OpenAIEntry({ apiKey }: { apiKey: string }) {
  const baseUrl = openaiBaseUrl();
  const examples = openaiExamples(baseUrl, apiKey);
  const [copied, setCopied] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  const copy = async (label: string, text: string) => {
    await navigator.clipboard.writeText(text);
    setCopied(label);
  };

  return (
    <Panel
      title="OpenAI 客户端接入"
      action={
        <Button size="sm" variant="ghost" data-testid="openai-entry-toggle" onClick={() => setOpen((value) => !value)}>
          <ChevronDown className={cn("size-4 transition-transform", open && "rotate-180")} />
          {open ? "收起" : "说明"}
        </Button>
      }
    >
      {open ? (
        <div className="space-y-4 text-sm" data-testid="openai-entry">
          <div>
            <div className="mb-1 text-xs text-muted-foreground">Base URL</div>
            <div className="flex items-center gap-2">
              <code
                data-testid="openai-base-url"
                className="flex-1 truncate rounded-lg border border-input bg-muted/40 px-3 py-2 font-mono text-xs transition-colors hover:border-ring"
              >
                {baseUrl}
              </code>
              <CopyButton label={copied === "url" ? "已复制" : "复制"} onCopy={() => void copy("url", baseUrl)} />
            </div>
          </div>
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
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">
          展开查看 Base URL、端点列表与可复制的接入示例。
        </p>
      )}
    </Panel>
  );
}

export function ApiKeysPage() {
  const { data, isLoading } = useApiKeys();
  const client = useQueryClient();
  const [name, setName] = useState("");
  const [binding, setBinding] = useState("");
  const [allowedIps, setAllowedIps] = useState("");
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const keys = data?.api_keys ?? [];

  const create = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    try {
      const result = await api.createApiKey({
        name,
        provider_binding: binding,
        allowed_ips: allowedIps.trim(),
      });
      setCreated(result);
      setName("");
      setBinding("");
      setAllowedIps("");
      setCopied(false);
      await client.invalidateQueries({ queryKey: ["admin"] });
    } catch {
      setError("创建失败：请检查渠道绑定与 IP 白名单格式");
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

  return (
    <div className="space-y-6" data-testid="api-keys-page">
      <PageHeader
        title="API Key 管理"
        description="创建外部客户端（ChatGPT Next Web、LobeChat、Cursor 等）接入用的 Key，协议与 OpenAI 兼容。Key 仅在创建时完整显示一次，请立即保存；用量按 Key 归属用户统计。"
        icon={<KeyRound className="size-5" />}
      />
      <OpenAIEntry apiKey={created?.api_key ?? ""} />

      <Panel title="创建 API Key">
        <form onSubmit={create} className="flex flex-wrap items-end gap-3">
          <div className="w-56">
            <Field label="名称（可选）">
              <Input
                value={name}
                data-testid="key-name"
                placeholder="例如 laptop"
                onChange={(event) => setName(event.target.value)}
              />
            </Field>
          </div>
          <div className="w-52">
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
          </div>
          <div className="w-72">
            {/* 提示写进 label 而不是 Field 的 hint：hint 会把该字段撑高，
                与同排其它控件在 items-end 下对不齐输入框 */}
            <Field label="来源 IP 白名单（逗号分隔 IP/CIDR，留空不限制）">
              <Input
                value={allowedIps}
                data-testid="key-allowed-ips"
                placeholder="例如 203.0.113.9,10.0.0.0/8"
                onChange={(event) => setAllowedIps(event.target.value)}
              />
            </Field>
          </div>
          <Button type="submit" variant="primary">
            <Plus className="mr-1 size-4" />
            创建
          </Button>
        </form>
        {error && (
          <div className="mt-3">
            <Notice tone="danger">{error}</Notice>
          </div>
        )}
      </Panel>

      {created && (
        <Panel title="新 Key 已创建">
          <Notice tone="warn">此 Key 只会显示这一次，请立即复制保存；关闭后无法再次查看。</Notice>
          <div className="mt-3 flex items-center gap-3">
            <code
              data-testid="new-key-plaintext"
              className="flex-1 break-all rounded-lg border border-input bg-muted/40 px-3 py-2 font-mono text-xs"
            >
              {created.api_key}
            </code>
            <CopyButton label={copied ? "已复制" : "复制"} onCopy={copy} />
            <Button size="sm" variant="ghost" onClick={() => setCreated(null)}>
              我已保存
            </Button>
          </div>
        </Panel>
      )}

      <Panel title="我的 API Key">
        {isLoading ? (
          <Empty>载入中…</Empty>
        ) : keys.length === 0 ? (
          <Empty data-testid="no-keys">还没有 API Key</Empty>
        ) : (
          <Table data-testid="keys-table">
            <TableHeader>
              <TableRow>
                <TableHead>名称</TableHead>
                <TableHead>Key</TableHead>
                <TableHead>渠道</TableHead>
                <TableHead>来源 IP</TableHead>
                <TableHead>创建时间</TableHead>
                <TableHead>最后使用</TableHead>
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
    </div>
  );
}
