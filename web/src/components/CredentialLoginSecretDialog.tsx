import { useState } from "react";
import { KeyRound } from "lucide-react";
import { api } from "../api/client";
import type { Credential } from "../api/types";
import { useDialogFocus } from "../hooks/useDialogFocus";
import { Button, Field, Input, Notice } from "../ui";

/**
 * 小浣熊「账号密码」设置对话框。
 *
 * 为什么需要它：上游每日 300 积分只在「当天第一次网页登录」时补发，而
 * `/refresh` 换不出新会话（只是原样换回同一张 token）。要让服务自己把这个
 * 登录做掉，就必须存一份账号密码（见「按需登录」开关与 `tasks/checkin.py`）。
 *
 * 账号密码加密落库、**不回显**（导出凭证也不含密码），所以这里永远从空白
 * 开始：留空两个字段提交 = 清除，按需登录随之停用，签到回落到「需人工重登」。
 */
export function CredentialLoginSecretDialog({
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
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const dialogRef = useDialogFocus<HTMLDivElement>(busy ? undefined : onClose);
  const saved = credential.login_secret === 1;

  const submit = async () => {
    const trimmedPhone = phone.trim();
    if ((trimmedPhone === "") !== (password === "")) {
      setLocalError("手机号与密码需同时填写，或同时留空以清除");
      return;
    }
    setLocalError(null);
    setBusy(true);
    try {
      await api.setCredentialLoginSecret(credential.id, trimmedPhone, password);
      onSaved(trimmedPhone
        ? "账号密码已保存：按需登录打开时会用它换新会话。"
        : "账号密码已清除：按需登录随之停用。");
    } catch (caught) {
      onError(caught instanceof Error ? caught.message : "账号密码保存失败");
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
        aria-label="设置账号密码"
        data-testid="credential-login-secret-dialog"
        onClick={(event) => event.stopPropagation()}
        className="max-h-[90vh] w-full max-w-xl animate-in overflow-y-auto rounded-xl border border-border bg-card p-5 shadow-xl fade-in-0 zoom-in-95"
      >
        <div className="mb-3 flex items-start justify-between gap-3">
          <div>
            <h2 className="flex items-center gap-1.5 text-sm font-semibold">
              <KeyRound className="size-4" /> 设置账号密码
            </h2>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {credential.nickname || credential.id.slice(0, 12)}
              {saved && <span className="ml-2 text-primary">（已保存）</span>}
            </p>
          </div>
          <Button
            size="icon"
            variant="ghost"
            aria-label="关闭"
            data-testid="credential-login-secret-close"
            disabled={busy}
            onClick={onClose}
          >
            <KeyRound className="size-4" />
          </Button>
        </div>

        <Field
          label="手机号"
          htmlFor="credential-login-phone-input"
          hint="网页端登录用的手机号（含国家码也可，提交前会归一）。"
        >
          <Input
            id="credential-login-phone-input"
            value={phone}
            autoComplete="off"
            data-testid="credential-login-phone-input"
            placeholder="13900000001"
            onChange={(event) => {
              setPhone(event.target.value);
              setLocalError(null);
            }}
          />
        </Field>

        <Field
          label="密码"
          htmlFor="credential-login-password-input"
          hint="至少 8 位（上游下限）。留空提交（与手机号一起留空）= 清除。"
        >
          <Input
            id="credential-login-password-input"
            type="password"
            value={password}
            autoComplete="new-password"
            data-testid="credential-login-password-input"
            onChange={(event) => {
              setPassword(event.target.value);
              setLocalError(null);
            }}
          />
        </Field>

        {localError && (
          <p className="mt-2 text-xs text-destructive"
             data-testid="credential-login-secret-error">
            {localError}
          </p>
        )}

        <Notice tone="muted">
          只用于「按需登录」：当天没拿到每日积分时登录一次换新会话（上游只在登录时
          补发当天 300）。密码加密落库、不回显，导出凭证也不包含它；每天每号最多
          登录一次，密码被上游拒绝即停手并提示重新登录。
        </Notice>

        <div className="mt-4 flex justify-end gap-2">
          <Button variant="ghost" disabled={busy} onClick={onClose}>
            取消
          </Button>
          <Button
            variant="primary"
            disabled={busy}
            data-testid="credential-login-secret-save"
            onClick={() => void submit()}
          >
            {busy ? "保存中…" : "保存"}
          </Button>
        </div>
      </div>
    </div>
  );
}