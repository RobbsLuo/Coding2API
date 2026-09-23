import { useState } from "react";
import { KeyRound, TriangleAlert } from "lucide-react";
import { api, ApiError } from "../api/client";
import { Button, Field, Input, Notice } from "../ui";

/** 后端要求的最短密码长度（与 src/api/admin_auth.py 的 MIN_PASSWORD_LENGTH 一致）。 */
export const MIN_PASSWORD_LENGTH = 8;

/**
 * 修改密码对话框。
 *
 * `required` 为 true 时用于「首登/被重置后强制改密」：不能取消、不能关遮罩，
 * 因为此时除改密外的端点全被后端 403 挡住，关掉也没有可用界面。
 *
 * 成功后后端会 bump session_epoch 并换发 Cookie，本组件直接刷新页面
 * 让 App 重新取会话，避免手上还攥着旧 epoch 的查询缓存。
 */
export function ChangePasswordDialog({
  required = false,
  onDone,
  onCancel,
}: {
  required?: boolean;
  onDone?: () => void;
  onCancel?: () => void;
}) {
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    if (newPassword.length < MIN_PASSWORD_LENGTH) {
      setError(`新密码至少 ${MIN_PASSWORD_LENGTH} 位`);
      return;
    }
    if (newPassword !== confirmPassword) {
      setError("两次输入的新密码不一致");
      return;
    }
    setPending(true);
    try {
      await api.changePassword(currentPassword, newPassword);
      if (onDone) {
        onDone();
      } else {
        window.location.href = "/";
      }
    } catch (caught) {
      const message = caught instanceof ApiError ? caught.message : "";
      setError(message === "current password is incorrect" ? "当前密码不正确" : "修改失败，请稍后重试");
      setPending(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/50 p-4">
      <form
        role="dialog"
        aria-modal="true"
        aria-label="修改密码"
        data-testid="change-password-dialog"
        onSubmit={submit}
        className="w-full max-w-sm rounded-xl border border-border bg-background p-5 shadow-lg"
      >
        <div className="mb-4 flex items-center gap-2">
          <KeyRound className="size-4 text-muted-foreground" />
          <h2 className="text-sm font-semibold">{required ? "请先修改密码" : "修改密码"}</h2>
        </div>
        {required && (
          <div className="mb-4">
            <Notice tone="warn">
              账号使用的是初始/重置密码，必须先设置新密码才能继续使用管理台。
            </Notice>
          </div>
        )}
        <div className="space-y-3">
          <Field label="当前密码">
            <Input
              type="password"
              value={currentPassword}
              autoComplete="current-password"
              data-testid="password-current"
              onChange={(event) => setCurrentPassword(event.target.value)}
            />
          </Field>
          <Field label={`新密码（至少 ${MIN_PASSWORD_LENGTH} 位）`}>
            <Input
              type="password"
              value={newPassword}
              autoComplete="new-password"
              data-testid="password-new"
              onChange={(event) => setNewPassword(event.target.value)}
            />
          </Field>
          <Field label="确认新密码">
            <Input
              type="password"
              value={confirmPassword}
              autoComplete="new-password"
              data-testid="password-confirm"
              onChange={(event) => setConfirmPassword(event.target.value)}
            />
          </Field>
          {error && (
            <div data-testid="password-error">
              <Notice tone="danger">
                <span className="inline-flex items-center gap-1.5">
                  <TriangleAlert className="size-3.5" />
                  {error}
                </span>
              </Notice>
            </div>
          )}
        </div>
        <div className="mt-5 flex justify-end gap-2">
          {!required && (
            <Button type="button" variant="ghost" onClick={onCancel} disabled={pending}>
              取消
            </Button>
          )}
          <Button type="submit" variant="primary" disabled={pending}>
            {pending ? "提交中…" : "确认修改"}
          </Button>
        </div>
      </form>
    </div>
  );
}
