import { useState } from "react";
import { api } from "../api/client";
import { useApiKeys, useQueryClient } from "../api/hooks";
import { formatTime } from "../api/display";
import type { ApiKeyCreated } from "../api/types";
import { Badge, Button, Empty, Field, Input, Notice, Panel } from "../ui";

/** OpenAI 兼容入口的 Base URL：本服务地址 + /v1 */
export const openaiBaseUrl = (): string => `${window.location.origin}/v1`;

/** 接入示例代码；apiKey 占位时用 sk-… */
export function openaiExamples(baseUrl: string, apiKey: string): {
  curl: string;
  python: string;
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
  };
}

/** OpenAI 客户端接入面板：Base URL + 端点 + 可复制的示例 */
function OpenAIEntry({ apiKey }: { apiKey: string }) {
  const baseUrl = openaiBaseUrl();
  const examples = openaiExamples(baseUrl, apiKey);
  const [copied, setCopied] = useState<string | null>(null);

  const copy = async (label: string, text: string) => {
    await navigator.clipboard.writeText(text);
    setCopied(label);
  };

  return (
    <Panel title="OpenAI 客户端接入">
      <div className="space-y-4 text-sm" data-testid="openai-entry">
        <div>
          <div className="mb-1 text-xs text-[var(--color-ink-muted)]">Base URL</div>
          <div className="flex items-center gap-2">
            <code
              data-testid="openai-base-url"
              className="flex-1 rounded-lg border border-[var(--color-border-soft)] bg-[var(--color-surface)] px-3 py-2 font-mono text-xs break-all"
            >
              {baseUrl}
            </code>
            <Button size="sm" data-testid="copy-base-url" onClick={() => copy("url", baseUrl)}>
              {copied === "url" ? "已复制" : "复制"}
            </Button>
          </div>
        </div>
        <ul className="space-y-1 text-xs text-[var(--color-ink-muted)]">
          <li>
            {/* 示例默认折叠：点开端点行查看 curl / SDK 调用方式 */}
            <details data-testid="example-details">
              <summary className="cursor-pointer select-none">
                <Badge>POST</Badge> <code>{baseUrl}/chat/completions</code>　对话补全（流式 / 非流式）
                <span className="ml-1">▸ 调用示例</span>
              </summary>
              <div className="mt-3 space-y-3">
                <div>
                  <div className="mb-1 flex items-center justify-between">
                    <span>curl 示例</span>
                    <Button size="sm" variant="ghost" data-testid="copy-curl" onClick={() => copy("curl", examples.curl)}>
                      {copied === "curl" ? "已复制" : "复制"}
                    </Button>
                  </div>
                  <pre data-testid="example-curl" className="overflow-x-auto rounded-lg border border-[var(--color-border-soft)] bg-[var(--color-surface)] p-3 font-mono text-xs">
                    {examples.curl}
                  </pre>
                </div>
                <div>
                  <div className="mb-1 flex items-center justify-between">
                    <span>OpenAI Python SDK</span>
                    <Button size="sm" variant="ghost" data-testid="copy-python" onClick={() => copy("python", examples.python)}>
                      {copied === "python" ? "已复制" : "复制"}
                    </Button>
                  </div>
                  <pre data-testid="example-python" className="overflow-x-auto rounded-lg border border-[var(--color-border-soft)] bg-[var(--color-surface)] p-3 font-mono text-xs">
                    {examples.python}
                  </pre>
                </div>
              </div>
            </details>
          </li>
          <li>
            <Badge>GET</Badge> <code>{baseUrl}/models</code>　模型列表
          </li>
        </ul>
        <Notice tone="muted">
          任何兼容 OpenAI 协议的客户端（ChatGPT Next Web、LobeChat、Cursor 等）
          都可以按上面的 Base URL + API Key 接入。模型名从 /v1/models 获取。
        </Notice>
      </div>
    </Panel>
  );
}

export function ApiKeysPage() {
  const { data, isLoading } = useApiKeys();
  const client = useQueryClient();
  const [name, setName] = useState("");
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const keys = data?.api_keys ?? [];

  const create = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    try {
      const result = await api.createApiKey(name);
      setCreated(result);
      setName("");
      setCopied(false);
      await client.invalidateQueries({ queryKey: ["admin"] });
    } catch {
      setError("创建失败");
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
          <Button type="submit" variant="primary">
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
              className="flex-1 rounded-lg border border-[var(--color-border-soft)] bg-[var(--color-surface)] px-3 py-2 font-mono text-xs break-all"
            >
              {created.api_key}
            </code>
            <Button size="sm" onClick={copy}>
              {copied ? "已复制" : "复制"}
            </Button>
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
          <table className="w-full text-sm" data-testid="keys-table">
            <thead>
              <tr className="text-left text-xs text-[var(--color-ink-muted)]">
                <th className="pb-2 font-medium">名称</th>
                <th className="pb-2 font-medium">Key</th>
                <th className="pb-2 font-medium">创建时间</th>
                <th className="pb-2 font-medium">最后使用</th>
                <th className="pb-2 font-medium" />
              </tr>
            </thead>
            <tbody>
              {keys.map((key) => (
                <tr key={key.id} className="border-t border-[var(--color-border-soft)]">
                  <td className="py-2">{key.name || "—"}</td>
                  <td className="py-2 font-mono text-xs">{key.preview}</td>
                  <td className="py-2 text-xs">{formatTime(key.created_at)}</td>
                  <td className="py-2 text-xs">
                    {key.last_used_at ? formatTime(key.last_used_at) : "从未使用"}
                  </td>
                  <td className="py-2 text-right">
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
                        删除
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
    </div>
  );
}
