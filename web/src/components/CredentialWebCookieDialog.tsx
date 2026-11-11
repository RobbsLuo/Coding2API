import { useState } from "react";
import { Cookie } from "lucide-react";
import { api } from "../api/client";
import type { Credential } from "../api/types";
import { useDialogFocus } from "../hooks/useDialogFocus";
import { Button, Field, Notice, Textarea } from "../ui";

/** 与后端 `WEB_COOKIE_MAX_LENGTH` 对齐（前端先拦一次，减少往返 400）。 */
export const MAX_WEB_COOKIE_LENGTH = 10_000;

/**
 * Qoder 网页 Cookie 录入对话框。
 *
 * 为什么需要它：Qoder 的 device token 只能打 `/api/v2/quota/usage`（两桶汇总，
 * 到期字段是 9999 哨兵）；**逐包到期**（"哪笔积分什么时候作废"）只有网页账号页
 * 的 `/api/v2/me/usages/big_model_credits` 才给，而它只认网页登录会话。
 *
 * Cookie 一旦落库即加密保存、**不回显**（导出凭证也不含它，避免多一处泄漏面）；
 * 因此这里永远从空白开始，留空提交 = 清除，额度探测回退到汇总口径。
 * 网页会话会过期（通常几天到数周），失效后探测自动回退，重新粘贴即可。
 */
export function CredentialWebCookieDialog({
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
  const [raw, setRaw] = useState("");
  const [busy, setBusy] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const dialogRef = useDialogFocus<HTMLDivElement>(busy ? undefined : onClose);
  const isCodeArts = credential.provider === "codearts";

  const submit = async () => {
    const cookie = raw.trim();
    if (cookie.length > MAX_WEB_COOKIE_LENGTH) {
      setLocalError(`Cookie 过长（上限 ${MAX_WEB_COOKIE_LENGTH} 字符）`);
      return;
    }
    setLocalError(null);
    setBusy(true);
    try {
      await api.setCredentialWebCookie(credential.id, cookie);
      onSaved(cookie
        ? "网页 Cookie 已保存，已在后台重新探测额度。"
        : "网页 Cookie 已清除，额度回退为汇总口径。");
    } catch (caught) {
      onError(caught instanceof Error ? caught.message : "网页 Cookie 保存失败");
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
        aria-label="设置网页 Cookie"
        data-testid="credential-web-cookie-dialog"
        onClick={(event) => event.stopPropagation()}
        className="max-h-[90vh] w-full max-w-xl animate-in overflow-y-auto rounded-xl border border-border bg-card p-5 shadow-xl fade-in-0 zoom-in-95"
      >
        <div className="mb-3 flex items-start justify-between gap-3">
          <div>
            <h2 className="flex items-center gap-1.5 text-sm font-semibold">
              <Cookie className="size-4" /> 设置网页 Cookie
            </h2>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {credential.nickname || credential.id.slice(0, 12)}
            </p>
          </div>
          <Button
            size="icon"
            variant="ghost"
            aria-label="关闭"
            data-testid="credential-web-cookie-close"
            disabled={busy}
            onClick={onClose}
          >
            <Cookie className="size-4" />
          </Button>
        </div>

        <Field
          label="Cookie"
          htmlFor="credential-web-cookie-input"
          hint={isCodeArts
            ? "登录 codearts.huaweicloud.com 后，在「套餐与用量 → 获取明细」页的 /portal/promptcenter/v1/ops/credit/overview 请求里复制完整 Cookie 请求头。留空提交即清除。"
            : "登录 qoder.cn 后，在账号用量页的 /api/v2/me/usages/big_model_credits 请求里复制完整 Cookie 请求头。留空提交即清除。"}
        >
          <Textarea
            id="credential-web-cookie-input"
            value={raw}
            rows={5}
            spellCheck={false}
            autoComplete="off"
            data-testid="credential-web-cookie-input"
            placeholder={isCodeArts
              ? "devclouddevuibjJ_SESSION_ID=…; 其余 Cookie 一并粘贴"
              : "qoder_session_cookie=…; 其余 Cookie 一并粘贴"}
            onChange={(event) => {
              setRaw(event.target.value);
              setLocalError(null);
            }}
          />
        </Field>

        {localError && (
          <p className="mt-2 text-xs text-destructive" data-testid="credential-web-cookie-error">
            {localError}
          </p>
        )}

        <Notice tone="muted">
          只用于读取额度明细（逐包/逐笔到期），不参与推理请求。Cookie 加密落库、
          不会回显，导出凭证也不包含它；网页会话过期后重新粘贴一次即可。
        </Notice>

        <div className="mt-4 flex justify-end gap-2">
          <Button variant="ghost" disabled={busy} onClick={onClose}>
            取消
          </Button>
          <Button
            variant="primary"
            disabled={busy}
            data-testid="credential-web-cookie-save"
            onClick={() => void submit()}
          >
            {busy ? "保存中…" : "保存"}
          </Button>
        </div>
      </div>
    </div>
  );
}