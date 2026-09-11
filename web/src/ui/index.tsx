import type { ReactNode } from "react";

/** 极简共享 UI 原语：不引入组件库，保持依赖为零。 */

export function Panel({ title, action, children }: { title?: string; action?: ReactNode; children: ReactNode }) {
  return (
    <section className="rounded-xl border border-[var(--color-border-soft)] bg-[var(--color-panel)] p-5">
      {(title || action) && (
        <header className="mb-4 flex items-center justify-between gap-3">
          {title && <h2 className="text-sm font-semibold tracking-wide">{title}</h2>}
          {action}
        </header>
      )}
      {children}
    </section>
  );
}

type Tone = "ok" | "warn" | "danger" | "muted" | "accent";

const TONE_CLASS: Record<Tone, string> = {
  ok: "bg-[var(--color-ok)]/12 text-[var(--color-ok)]",
  warn: "bg-[var(--color-warn)]/15 text-[var(--color-warn)]",
  danger: "bg-[var(--color-danger)]/12 text-[var(--color-danger)]",
  muted: "bg-[var(--color-ink-muted)]/12 text-[var(--color-ink-muted)]",
  accent: "bg-[var(--color-accent)]/12 text-[var(--color-accent)]",
};

export function Badge({ tone = "muted", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${TONE_CLASS[tone]}`}>
      {children}
    </span>
  );
}

export function Button({
  children,
  variant = "default",
  size = "md",
  ...rest
}: {
  children: ReactNode;
  variant?: "default" | "primary" | "danger" | "ghost";
  size?: "sm" | "md";
} & Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "className">) {
  const base = "inline-flex items-center justify-center rounded-lg font-medium disabled:opacity-40 disabled:cursor-not-allowed";
  const sizes = size === "sm" ? "px-2.5 py-1 text-xs" : "px-3.5 py-2 text-sm";
  const variants = {
    default: "border border-[var(--color-border-soft)] hover:bg-[var(--color-surface)]",
    primary: "bg-[var(--color-accent)] text-white hover:opacity-90",
    danger: "border border-[var(--color-danger)]/40 text-[var(--color-danger)] hover:bg-[var(--color-danger)]/10",
    ghost: "hover:bg-[var(--color-surface)]",
  };
  return (
    <button {...rest} type={rest.type ?? "button"} className={`${base} ${sizes} ${variants[variant]}`}>
      {children}
    </button>
  );
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="block space-y-1.5">
      <span className="text-xs font-medium text-[var(--color-ink-muted)]">{label}</span>
      {children}
      {hint && <span className="block text-xs text-[var(--color-ink-muted)]">{hint}</span>}
    </label>
  );
}

export function Input(props: React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      {...props}
      className={`w-full rounded-lg border border-[var(--color-border-soft)] bg-[var(--color-surface)] px-3 py-2 text-sm outline-none focus:border-[var(--color-accent)] ${props.className ?? ""}`}
    />
  );
}

export function Textarea(props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      {...props}
      className={`w-full rounded-lg border border-[var(--color-border-soft)] bg-[var(--color-surface)] px-3 py-2 font-mono text-xs outline-none focus:border-[var(--color-accent)] ${props.className ?? ""}`}
    />
  );
}

export function Select(props: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      {...props}
      className={`rounded-lg border border-[var(--color-border-soft)] bg-[var(--color-surface)] px-3 py-2 text-sm outline-none focus:border-[var(--color-accent)] ${props.className ?? ""}`}
    />
  );
}

export function Empty({
  children,
  ...rest
}: { children: ReactNode } & React.HTMLAttributes<HTMLParagraphElement>) {
  return (
    <p {...rest} className="py-8 text-center text-sm text-[var(--color-ink-muted)]">
      {children}
    </p>
  );
}

export function Notice({ tone = "muted", children }: { tone?: Tone; children: ReactNode }) {
  return <div className={`rounded-lg px-3 py-2 text-xs ${TONE_CLASS[tone]}`}>{children}</div>;
}

export function Metric({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="rounded-xl border border-[var(--color-border-soft)] bg-[var(--color-panel)] px-4 py-3">
      <div className="text-xs text-[var(--color-ink-muted)]">{label}</div>
      <div className="mt-1 text-xl font-semibold tabular-nums">{value}</div>
      {hint && <div className="mt-0.5 text-xs text-[var(--color-ink-muted)]">{hint}</div>}
    </div>
  );
}
