import { useState } from "react";
import { api } from "../api/client";
import { useApiKeys, useQueryClient } from "../api/hooks";
import { formatTime } from "../api/display";
import type { ApiKeyCreated } from "../api/types";
import { Button, Empty, Field, Input, Notice, Panel } from "../ui";

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
