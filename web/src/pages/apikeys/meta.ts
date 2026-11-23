import { PROVIDER_LABEL, PROVIDER_ORDER } from "../../api/providers";

/** 渠道绑定的下拉选项：值与后端 KNOWN_PROVIDERS 对齐，空串 = 自动。 */
export const PROVIDER_BINDING_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "自动（不限定渠道）" },
  // 渠道集合与展示名都来自 providers.ts 的单一来源，新增渠道不再改这里
  ...PROVIDER_ORDER.map((provider) => ({ value: provider, label: PROVIDER_LABEL[provider] })),
];

/** 列表里展示绑定渠道：空串 → 「自动」 */
export function providerBindingLabel(binding: string | null | undefined): string {
  if (!binding) return "自动";
  return PROVIDER_BINDING_OPTIONS.find((o) => o.value === binding)?.label ?? binding;
}

/** `datetime-local` 值（本地时区的 `YYYY-MM-DDTHH:mm`）→ epoch 秒；空串 = 永不过期。 */
export function parseExpiresAt(value: string): number | null {
  const raw = value.trim();
  if (!raw) return null;
  const ms = new Date(raw).getTime();
  if (!Number.isFinite(ms)) throw new Error("invalid expires_at");
  return Math.floor(ms / 1000);
}

/**
 * epoch 秒 → `datetime-local` 输入框的本地时间文本（`parseExpiresAt` 的逆）。
 *
 * 必须用**本地时间**：`datetime-local` 不含时区，直接 `toISOString()` 会按
 * UTC 显示，编辑框回显的时间比实际早/晚一个时区，用户一保存就把到期时间改错。
 */
export function expiresAtInput(expiresAt: number | null | undefined): string {
  if (!expiresAt) return "";
  const date = new Date(expiresAt * 1000);
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
    + `T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
