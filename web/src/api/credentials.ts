import { ApiError } from "./client";
import type { Credential, CredentialLimits } from "./types";

/**
 * 凭证编辑的**乐观并发**（v39 / P2-19）与账号级模型白名单（P2-20）。
 *
 * 为什么不塞进 `client.ts` 的 `api` 对象：这个端点不只是「成功/失败」两态，
 * 还要把 409 的**当前快照**原样交给 UI（提示「已被他人修改」并让用户刷新）。
 * `request()` 会对所有非 2xx 抛 `ApiError`，把 409 的 body 丢掉；这里单独走
 * 一次 fetch 就能把冲突结果作为返回值带回。
 *
 * 协议（与后端 `PUT /api/credentials/{id}/limits` 一一对应）：
 * - 请求体：`{ keep_credits, daily_tokens, daily_credits, per_model_tokens,
 *   allowed_models, expected_updated_at }`
 * - 200：`{ limits, allowed_models, updated_at }`
 * - 409：`{ error: {code: "credential_conflict"}, current: {...} }` →
 *   返回 `{ kind: "conflict", current }`，调用方提示刷新
 */

/** 列表行上的编辑元数据（`GET /api/credentials` 每行都带）。 */
export interface CredentialEditMeta {
  /** 编辑版本号：提交时回传，服务端据此判定「我拿到的是不是最新版本」 */
  updated_at: number;
  /** 账号级模型白名单（fnmatch glob，逗号分隔，'' = 不限制） */
  allowed_models: string;
}

/**
 * 从列表行读编辑元数据。
 *
 * `Credential` 是共享类型（`api/types.ts` 由多个模块共同维护），这里不直接
 * 扩展它，而是用局部扩展 + 默认值读取：字段缺失（旧后端 / 测试替身）时按
 * 「版本 0、不限制」处理，与后端补列默认值一致。
 */
export function editMeta(row: Credential): CredentialEditMeta {
  // `Credential` 的共享定义（`api/types.ts`）由多个模块共同维护，这里不改它，
  // 而是在本模块声明 v39 才出现的两个字段：缺失（旧后端 / 测试替身）时按
  // 「版本 0、不限制」处理，与后端补列默认值一致。
  const raw = row as Credential & {
    updated_at?: number;
    allowed_models?: string;
  };
  return {
    updated_at: raw.updated_at ?? 0,
    allowed_models: raw.allowed_models ?? "",
  };
}

/** 409 响应里的当前快照（后端 `current` 字段）。 */
export interface CredentialConflictSnapshot {
  id: string;
  updated_at: number;
  allowed_models: string;
  limits: CredentialLimits | null;
}

export interface CredentialLimitsSaveInput {
  keep_credits: number | null;
  daily_tokens: number | null;
  daily_credits: number | null;
  per_model_tokens: Record<string, number>;
  /** 账号级模型白名单（'' = 不限制） */
  allowed_models: string;
  /**
   * 打开编辑器时看到的版本号。
   *
   * **管理台必须传**（否则冲突检测形同虚设——后端把它当「不传 = 兼容旧脚本」
   * 的旧行为，直接最后写入者赢）。`null` 只给「确实要强制覆盖」的脚本用。
   */
  expected_updated_at: number | null;
}

export type CredentialLimitsSaveResult =
  | { kind: "ok"; limits: CredentialLimits | null;
      allowed_models: string; updated_at: number }
  | { kind: "conflict"; current: CredentialConflictSnapshot };

export async function saveCredentialLimits(
  id: string,
  input: CredentialLimitsSaveInput,
): Promise<CredentialLimitsSaveResult> {
  const response = await fetch(
    `/api/credentials/${encodeURIComponent(id)}/limits`,
    {
      method: "PUT",
      credentials: "same-origin",
      headers: {
        // 与 `client.ts` 同一 CSRF 纵深：服务端要求写请求带自定义头
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(input),
    },
  );
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // 非 JSON 错误体（反代 HTML 错误页）：保留 HTTP 状态提示
  }
  if (response.status === 409) {
    const conflict = body as { current?: CredentialConflictSnapshot } | null;
    return {
      kind: "conflict",
      current: conflict?.current ?? {
        id, updated_at: 0, allowed_models: "", limits: null,
      },
    };
  }
  if (!response.ok) {
    const error = (body as { error?: { code?: string; message?: string } } | null)?.error;
    throw new ApiError(
      response.status,
      error?.code ?? "unknown",
      error?.message || response.statusText || `HTTP ${response.status}`,
    );
  }
  return body as CredentialLimitsSaveResult & { kind: "ok" };
}
