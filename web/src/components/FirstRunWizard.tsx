import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Check, KeyRound, Rocket, Server } from "lucide-react";
import { useCredentials } from "../api/hooks";
import { useDialogFocus } from "../hooks/useDialogFocus";
import { Button } from "./ui/button";

const STORAGE_PREFIX = "coding2api.onboarding.v1";

const STEPS = [
  {
    title: "添加第一个上游账号",
    description:
      "凭证管理页支持粘贴 Bearer / Cookie / 登录态等渠道凭据；没有可用账号时网关无法转发请求。",
    action: "去添加凭证",
  },
  {
    title: "创建 API Key",
    description:
      "给外部客户端（ChatGPT Next Web、LobeChat、Cursor 等）发放 Key。Key 只在创建时完整显示一次。",
    action: "去创建 Key",
  },
  {
    title: "调用一次，验证链路",
    description:
      "用任意 OpenAI 兼容客户端指向本站 /v1；下面的 curl 可直接验证。",
    action: "完成",
  },
] as const;

/**
 * 首启向导：admin 且**一个上游凭证都没有**时弹一次，三步走完或跳过后不再出现。
 *
 * 完成状态按用户存 localStorage（`coding2api.onboarding.v1:<username>`）：
 * 后端没有「是否已完成引导」的字段，为一个纯前端引导加迁移不划算。
 * 挂在 `App` 而不是 `Layout`：页面级测试用 Layout 单独渲染，不会被弹窗干扰。
 */
export function FirstRunWizard({ username, isAdmin }: {
  username: string;
  isAdmin: boolean;
}) {
  const storageKey = `${STORAGE_PREFIX}:${username}`;
  const [dismissed, setDismissed] = useState(
    () => localStorage.getItem(storageKey) === "done",
  );
  if (!isAdmin || dismissed) return null;
  return (
    <WizardGate
      onDismiss={() => {
        localStorage.setItem(storageKey, "done");
        setDismissed(true);
      }}
    />
  );
}

function WizardGate({ onDismiss }: { onDismiss: () => void }) {
  const navigate = useNavigate();
  const credentials = useCredentials();
  const [open, setOpen] = useState(true);
  const [step, setStep] = useState(0);
  const dialogRef = useDialogFocus<HTMLDivElement>(onDismiss, open);

  // 已经有凭证 = 不是首次启动（或已完成第一步）：不打扰
  const fresh = credentials.data?.credentials.length === 0;
  if (!open || !fresh) return null;

  const current = STEPS[step];
  const go = (path: string) => {
    // 关掉向导再跳转：目标页会按 ?new=1 自动打开新增对话框，避免两层弹窗
    setOpen(false);
    navigate(path);
  };
  const finish = () => {
    onDismiss();
    setOpen(false);
  };

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/50 p-4 backdrop-blur-sm">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label="首次使用向导"
        data-testid="first-run-wizard"
        className="w-full max-w-lg space-y-4 rounded-xl border border-border bg-card p-5 shadow-xl"
      >
        <div className="flex items-center gap-2">
          <Rocket className="size-4 text-muted-foreground" />
          <h2 className="text-sm font-semibold">开始使用 Coding2API</h2>
          <span className="ml-auto text-xs text-muted-foreground" data-testid="wizard-progress">
            第 {step + 1} / {STEPS.length} 步
          </span>
        </div>

        <ol className="flex gap-1.5">
          {STEPS.map((item, index) => (
            <li
              key={item.title}
              aria-current={index === step ? "step" : undefined}
              className={`h-1 flex-1 rounded-full ${
                index <= step ? "bg-[var(--color-primary,#2563eb)]" : "bg-muted"
              }`}
            />
          ))}
        </ol>

        <div className="space-y-1.5">
          <div className="flex items-center gap-2 text-sm font-medium">
            {step === 1 ? <KeyRound className="size-4" /> : <Server className="size-4" />}
            {current.title}
          </div>
          <p className="text-sm text-muted-foreground">{current.description}</p>
        </div>

        {step === STEPS.length - 1 && (
          <pre
            data-testid="wizard-curl"
            className="overflow-auto rounded-md bg-muted p-3 text-xs whitespace-pre-wrap break-words"
          >
{`curl ${window.location.origin}/v1/chat/completions \\
  -H "Authorization: Bearer <你的 API Key>" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"hi"}]}'`}
          </pre>
        )}

        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            variant="ghost"
            data-testid="wizard-skip"
            onClick={finish}
          >
            跳过，不再提示
          </Button>
          <div className="ml-auto flex gap-2">
            {step > 0 && (
              <Button
                size="sm"
                variant="ghost"
                data-testid="wizard-back"
                onClick={() => setStep(step - 1)}
              >
                上一步
              </Button>
            )}
            {step < STEPS.length - 1 ? (
              <>
                <Button
                  size="sm"
                  variant="ghost"
                  data-testid="wizard-next"
                  onClick={() => setStep(step + 1)}
                >
                  稍后配置
                </Button>
                <Button
                  size="sm"
                  variant="default"
                  data-testid="wizard-action"
                  onClick={() => go(step === 0 ? "/?new=1" : "/api-keys?new=1")}
                >
                  {current.action}
                </Button>
              </>
            ) : (
              <Button
                size="sm"
                variant="default"
                data-testid="wizard-finish"
                onClick={finish}
              >
                <Check className="size-3.5" /> 完成
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
