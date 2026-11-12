import { useState } from "react";
import { Gauge, X } from "lucide-react";
import { api } from "../api/client";
import type { Credential } from "../api/types";
import { useDialogFocus } from "../hooks/useDialogFocus";
import { Button, Field, Input, Notice } from "../ui";

/** 与后端 `src/api/admin_credentials.py` 的 WEIGHT_MIN / WEIGHT_MAX 对齐。 */
export const MIN_WEIGHT = -1000;
export const MAX_WEIGHT = 1000;

/** 空串 / `default` → null（恢复默认）；非法或越界 → "invalid"。 */
export function parseWeightInput(raw: string): number | null | "invalid" {
  const text = raw.trim();
  if (!text || text.toLowerCase() === "default") return null;
  if (!/^-?\d+$/.test(text)) return "invalid";
  const value = Number(text);
  if (!Number.isSafeInteger(value) || value < MIN_WEIGHT || value > MAX_WEIGHT) {
    return "invalid";
  }
  return value;
}

/**
 * 调度权重编辑对话框。
 *
 * 权重只改**同一健康分档内**的选号次序：正数越优先，负数越靠后（备用号），
 * 0/留空 = 默认。它压不过健康分档本身——exhausted 的号即使权重再高也不会
 * 被选中，所以提示里必须说清「不是准入条件」。
 */
export function CredentialWeightDialog({
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
  const current = credential.routing_weight;
  const [raw, setRaw] = useState(current === null || current === 0 ? "" : String(current));
  const [busy, setBusy] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const dialogRef = useDialogFocus<HTMLDivElement>(busy ? undefined : onClose);

  const parsed = parseWeightInput(raw);

  const submit = async () => {
    if (parsed === "invalid") {
      setLocalError(`权重需为 ${MIN_WEIGHT} ~ ${MAX_WEIGHT} 的整数，留空 = 默认`);
      return;
    }
    setLocalError(null);
    setBusy(true);
    try {
      await api.setCredentialWeight(credential.id, parsed);
      onSaved(parsed === null ? "权重已恢复默认。" : `权重已设为 ${parsed}。`);
    } catch (caught) {
      onError(caught instanceof Error ? caught.message : "权重保存失败");
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
        aria-label="设置调度权重"
        data-testid="credential-weight-dialog"
        onClick={(event) => event.stopPropagation()}
        className="max-h-[90vh] w-full max-w-md animate-in overflow-y-auto rounded-xl border border-border bg-card p-5 shadow-xl fade-in-0 zoom-in-95"
      >
        <div className="mb-3 flex items-start justify-between gap-3">
          <div>
            <h2 className="flex items-center gap-1.5 text-sm font-semibold">
              <Gauge className="size-4" /> 设置调度权重
            </h2>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {credential.nickname || credential.id.slice(0, 12)}
            </p>
          </div>
          <Button
            size="icon"
            variant="ghost"
            aria-label="关闭"
            data-testid="credential-weight-close"
            disabled={busy}
            onClick={onClose}
          >
            <X className="size-4" />
          </Button>
        </div>

        <Field
          label="权重"
          htmlFor="credential-weight-input"
          hint={`${MIN_WEIGHT} ~ ${MAX_WEIGHT} 的整数；正数越优先，负数当备用号，留空 = 默认。`}
        >
          <Input
            id="credential-weight-input"
            value={raw}
            inputMode="numeric"
            data-testid="credential-weight-input"
            placeholder="默认"
            onChange={(event) => {
              setRaw(event.target.value);
              setLocalError(null);
            }}
          />
        </Field>

        {localError && (
          <p className="mt-2 text-xs text-destructive" data-testid="credential-weight-error">
            {localError}
          </p>
        )}

        <Notice tone="muted">
          权重只影响同类健康号之间的先后，不会把不可用（冷却中 / 已耗尽 / 已停用）
          的号顶上来。高权重会压过免费优先与在途分摊，建议小幅调整。
        </Notice>

        <div className="mt-4 flex justify-end gap-2">
          <Button variant="ghost" disabled={busy} onClick={onClose}>
            取消
          </Button>
          <Button
            variant="primary"
            disabled={busy}
            data-testid="credential-weight-save"
            onClick={() => void submit()}
          >
            {busy ? "保存中…" : "保存"}
          </Button>
        </div>
      </div>
    </div>
  );
}
