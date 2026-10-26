import { useState } from "react";
import { Tag, X } from "lucide-react";
import { api } from "../api/client";
import type { Credential } from "../api/types";
import { useDialogFocus } from "../hooks/useDialogFocus";
import { Button, Field, Input, Notice } from "../ui";

/** 与后端 `src/tags.py` 的 MAX_TAGS / MAX_TAG_LENGTH 对齐（前端先拦一次，减少往返 400）。 */
export const MAX_TAGS = 16;
export const MAX_TAG_LENGTH = 24;

/**
 * 逗号分隔文本 → 标签列表。
 *
 * 与后端同口径：半角/全角逗号与分号都当分隔符（中文输入法下极易打出全角逗号），
 * 去空白、去重（大小写不敏感、保留首次写法）、丢弃含分隔符的畸形项。
 * 前端先做一遍是为了即时反馈——但**不**以它为准，后端仍会再校验一次。
 */
export function parseTagInput(raw: string): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const piece of raw.split(/[,，;；]/)) {
    const tag = piece.trim();
    if (!tag) continue;
    const key = tag.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(tag);
  }
  return out;
}

/**
 * 校验解析后的标签；返回中文提示，null = 通过。
 *
 * 三条限制与后端一一对应，文案里带上限值，用户不必去翻文档才知道为什么被拒。
 */
export function validateTags(tags: string[]): string | null {
  if (tags.length > MAX_TAGS) return `标签最多 ${MAX_TAGS} 条`;
  for (const tag of tags) {
    if (tag.length > MAX_TAG_LENGTH) {
      return `单个标签最长 ${MAX_TAG_LENGTH} 个字符：「${tag.slice(0, MAX_TAG_LENGTH)}…」`;
    }
  }
  return null;
}

/**
 * 凭证标签编辑对话框。
 *
 * 标签是纯标记：不进调度、不进鉴权，只用于列表筛选。因此保存后不需要「重新选号
 * 才生效」这类提示——改完刷新列表即可看到。
 */
export function CredentialTagsDialog({
  credential,
  onClose,
  onSaved,
  onError,
}: {
  credential: Credential;
  onClose: () => void;
  onSaved: (message: string) => void;
  onError: (message: string) => void;
}) {
  const [raw, setRaw] = useState((credential.tags ?? []).join(", "));
  const [busy, setBusy] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const dialogRef = useDialogFocus<HTMLDivElement>(busy ? undefined : onClose);

  const parsed = parseTagInput(raw);

  const submit = async () => {
    const message = validateTags(parsed);
    if (message) {
      setLocalError(message);
      return;
    }
    setLocalError(null);
    setBusy(true);
    try {
      await api.setCredentialTags(credential.id, parsed);
      onSaved(parsed.length ? `标签已保存（${parsed.join(" / ")}）。` : "标签已清空。");
    } catch (caught) {
      onError(caught instanceof Error ? caught.message : "标签保存失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center bg-black/50 p-4 backdrop-blur-sm"
      onClick={() => !busy && onClose()}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label="设置标签"
        data-testid="credential-tags-dialog"
        onClick={(event) => event.stopPropagation()}
        className="max-h-[90vh] w-full max-w-md animate-in overflow-y-auto rounded-xl border border-border bg-card p-5 shadow-xl fade-in-0 zoom-in-95"
      >
        <div className="mb-3 flex items-start justify-between gap-3">
          <div>
            <h2 className="flex items-center gap-1.5 text-sm font-semibold">
              <Tag className="size-4" /> 设置标签
            </h2>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {credential.nickname || credential.id.slice(0, 12)}
            </p>
          </div>
          <Button
            size="icon"
            variant="ghost"
            aria-label="关闭"
            data-testid="credential-tags-close"
            disabled={busy}
            onClick={onClose}
          >
            <X className="size-4" />
          </Button>
        </div>

        <Field
          label="标签"
          htmlFor="credential-tags-input"
          hint={`逗号分隔，最多 ${MAX_TAGS} 个、每个不超 ${MAX_TAG_LENGTH} 字符；留空即清除。标签只用于筛选，不影响调度。`}
        >
          <Input
            id="credential-tags-input"
            value={raw}
            data-testid="credential-tags-input"
            placeholder="生产, 备用, 低倍率"
            onChange={(event) => {
              setRaw(event.target.value);
              setLocalError(null);
            }}
          />
        </Field>

        {localError && (
          <p className="mt-2 text-xs text-destructive" data-testid="credential-tags-error">
            {localError}
          </p>
        )}

        {parsed.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1" data-testid="credential-tags-preview">
            {parsed.map((tag) => (
              <span
                key={tag}
                className="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground"
              >
                {tag}
              </span>
            ))}
          </div>
        )}

        <Notice tone="muted">
          标签是给运维看的记号，不参与选号、不改变请求行为。
        </Notice>

        <div className="mt-4 flex justify-end gap-2">
          <Button variant="ghost" disabled={busy} onClick={onClose}>
            取消
          </Button>
          <Button
            variant="primary"
            disabled={busy}
            data-testid="credential-tags-save"
            onClick={() => void submit()}
          >
            {busy ? "保存中…" : "保存"}
          </Button>
        </div>
      </div>
    </div>
  );
}
