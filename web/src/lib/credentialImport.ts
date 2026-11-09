import { providerLabel } from "../api/providers";
import type { Provider } from "../api/types";

/**
 * 「JSON 导入」输入解析：把用户粘贴的东西归一成 `{provider, credential}`。
 *
 * 支持四种粘法，目的是省掉「先选渠道、再手拼 JSON」这一步：
 * 1. 渠道 JSON 对象（`{"token":"…"}`、`{"accessToken":…,"uid":…}` 等）；
 * 2. 整段 Cookie（`wps_sid=…; uid=…`，可带 `Cookie:` 前缀）；
 * 3. 裸 token（JWT / bearer）；
 * 4. DevTools 的「Copy as cURL」整段命令——里面带 `Cookie:` / `Authorization:`
 *    头，HttpOnly cookie 也能拿到，这是浏览器里唯一零安装的取凭据方式。
 *
 * 渠道识别只认**无歧义特征**：`wps_sid` → 灵犀、`xhx_token` 或
 * `iss=xiaohuanxiong` → 小浣熊、`accessToken`/`refreshToken` → TRAE、
 * AK/SK → CodeArts、`model_app_key` → OfficeAce。CodeBuddy / Qoder 的
 * `token` / `access_token` 长得一样，故意不猜，沿用下拉选中的渠道——猜错
 * 会把凭证落到错池，直到一次 401 才暴露。
 */

/** 手工导入下拉里的渠道；zen / kilo 无凭证，不在其中。 */
export const IMPORT_PROVIDERS: Provider[] = [
  "codebuddy",
  "codebuddy_intl",
  "trae",
  "qoder",
  "codearts",
  "officeace",
  "raccoon",
  "lingxi",
];

export type ParsedCredential =
  | {
      ok: true;
      /** 实际用于导入的渠道（识别优先，其次下拉选中）。 */
      provider: Provider;
      credential: Record<string, unknown>;
      /** 识别来源，供界面提示用。 */
      detected: Provider | null;
    }
  | { ok: false; error: string };

/** JWT payload；解不开返回 null（不验签，只读元数据）。 */
function jwtClaims(token: string): Record<string, unknown> | null {
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  try {
    const base64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const padded = base64.padEnd(Math.ceil(base64.length / 4) * 4, "=");
    const binary = atob(padded);
    const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
    const text = new TextDecoder().decode(bytes);
    const claims: unknown = JSON.parse(text);
    return typeof claims === "object" && claims !== null
      ? (claims as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

/** 从 JWT 的 `iss` 认小浣熊；其余 token 一律不猜。 */
function providerFromToken(token: string): Provider | null {
  const claims = jwtClaims(token.trim());
  const issuer = claims?.iss;
  if (typeof issuer === "string" && issuer.toLowerCase().includes("xiaohuanxiong")) {
    return "raccoon";
  }
  return null;
}

/** Cookie 头里的特征键；只认能唯一定位渠道的。 */
function providerFromCookie(cookie: string): Provider | null {
  const pairs = cookie
    .split(";")
    .map((part) => part.split("=")[0]?.trim().toLowerCase())
    .filter(Boolean);
  if (pairs.includes("wps_sid")) return "lingxi";
  if (pairs.includes("xhx_token")) return "raccoon";
  return null;
}

function providerFromObject(value: Record<string, unknown>): Provider | null {
  const has = (key: string) => Object.prototype.hasOwnProperty.call(value, key);
  if (has("accessToken") || has("refreshToken")) return "trae";
  if (has("access_key_id") || has("secret_access_key")) return "codearts";
  if (has("model_app_key") || has("model_app_secret") || has("model_base_url")) {
    return "officeace";
  }
  const provider = value.provider;
  if (typeof provider === "string" && IMPORT_PROVIDERS.includes(provider as Provider)) {
    return provider as Provider;
  }
  return null;
}

/**
 * 从 cURL 命令里抽 Cookie / Authorization。
 *
 * 只解析 `-H/--header`、`-b/--cookie` 的引号参数：Chrome / Edge 的
 * 「Copy as cURL」就是这种形态。拿不到就返回 null，交给后面的普通解析。
 */
export function headersFromCurl(text: string): {
  cookie: string | null;
  authorization: string | null;
} {
  const cookie: string[] = [];
  let authorization: string | null = null;
  const pattern = /(?:^|\s)(?:-H|--header|-b|--cookie)\s+(['"])([\s\S]*?)\1/g;
  for (const match of text.matchAll(pattern)) {
    const value = match[2];
    const colon = value.indexOf(":");
    if (colon === -1) {
      // `-b 'a=1; b=2'`：整个参数就是 Cookie 值
      cookie.push(value);
      continue;
    }
    const name = value.slice(0, colon).trim().toLowerCase();
    const headerValue = value.slice(colon + 1).trim();
    if (name === "cookie") cookie.push(headerValue);
    if (name === "authorization") authorization = headerValue;
  }
  return { cookie: cookie.length ? cookie.join("; ") : null, authorization };
}

/** 看起来像 Cookie 头：至少一个 `name=value`，且只由 cookie 字符组成。 */
function looksLikeCookie(text: string): boolean {
  const trimmed = text.trim();
  if (!/^[A-Za-z0-9_\-]+=[^;\s]*(\s*;\s*[A-Za-z0-9_\-]+=[^;\s]*)*$/.test(trimmed)) {
    return false;
  }
  return providerFromCookie(trimmed) !== null || /[;]/.test(trimmed);
}

/** 裸 token：无空白、非 JSON、通常以 `eyJ` 开头（JWT）或足够长的随机串。 */
function looksLikeToken(text: string): boolean {
  const trimmed = text.trim();
  if (!trimmed || /\s/.test(trimmed)) return false;
  if (trimmed.startsWith("{") || trimmed.startsWith("[") || trimmed.startsWith("curl")) {
    return false;
  }
  return trimmed.length >= 16;
}

/** 按渠道把裸 token / Cookie 包成该渠道的最小凭证对象。 */
function wrapRaw(
  provider: Provider,
  source: { cookie: string | null; token: string | null },
  recognized: boolean,
): ParsedCredential {
  const detected = recognized ? provider : null;
  if (source.cookie !== null) {
    if (provider === "lingxi" || provider === "raccoon") {
      return {
        ok: true,
        provider,
        credential: { cookie: source.cookie },
        detected,
      };
    }
    return {
      ok: false,
      error: `${providerLabel(provider)} 不使用 Cookie 登录：请在 DevTools 里复制 Authorization 头或该渠道的 JSON 凭证`,
    };
  }
  const token = source.token ?? "";
  if (provider === "raccoon") {
    return { ok: true, provider, credential: { access_token: token }, detected };
  }
  if (provider === "lingxi") {
    return { ok: true, provider, credential: { cookie: token }, detected };
  }
  if (provider === "codebuddy" || provider === "codebuddy_intl" || provider === "qoder") {
    return { ok: true, provider, credential: { token }, detected };
  }
  return {
    ok: false,
    error: `${providerLabel(provider)} 需要完整 JSON 凭证（含多个字段），不能只粘一个 token`,
  };
}

/**
 * 解析导入框内容。
 *
 * @param text 用户粘贴的原文
 * @param fallback 下拉选中的渠道；识别不出时用它
 */
export function parseCredentialInput(
  text: string,
  fallback: Provider,
  detect = true,
): ParsedCredential {
  const trimmed = text.trim();
  if (!trimmed) {
    return { ok: false, error: "请先粘贴凭证 JSON、Cookie 或 DevTools 的 Copy as cURL" };
  }

  // cURL：先抽头，再走和裸内容相同的分支
  if (/^curl(\s|$)/i.test(trimmed)) {
    const { cookie, authorization } = headersFromCurl(trimmed);
    const bearer = authorization?.replace(/^Bearer\s+/i, "") ?? null;
    if (!cookie && !bearer) {
      return {
        ok: false,
        error: "这条 cURL 里没有 Cookie 或 Authorization 头，换一条带登录态的请求再复制",
      };
    }
    const token = bearer?.trim() || null;
    const detected = !detect
      ? null
      : cookie
        ? providerFromCookie(cookie)
        : token
          ? providerFromToken(token)
          : null;
    return wrapRaw(detected ?? fallback, { cookie, token }, detected !== null);
  }

  // JSON：单个对象继续；数组提示走批量导入
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(trimmed);
    } catch {
      return {
        ok: false,
        error: "凭证必须是合法 JSON（也可粘贴整段 Cookie 或 Copy as cURL）",
      };
    }
    if (Array.isArray(parsed)) {
      return {
        ok: false,
        error: "检测到多条凭证：请用凭证页右上角的「批量导入」",
      };
    }
    if (typeof parsed !== "object" || parsed === null) {
      return {
        ok: false,
        error: "凭证必须是合法 JSON 对象（也可粘贴整段 Cookie 或 Copy as cURL）",
      };
    }
    const object = parsed as Record<string, unknown>;
    // 批量导入同形的 `{provider, credential}` 也收：用户从导出/脚本拿到的
    // 就是这种形态，没必要先删掉外层。
    const nested = object.credential;
    if (typeof nested === "object" && nested !== null && !Array.isArray(nested)) {
      const declared = object.provider;
      const declaredProvider =
        typeof declared === "string" && IMPORT_PROVIDERS.includes(declared as Provider)
          ? (declared as Provider)
          : providerFromObject(object);
      return {
        ok: true,
        provider: declaredProvider ?? fallback,
        credential: nested as Record<string, unknown>,
        detected: declaredProvider,
      };
    }
    const detected = detect ? providerFromObject(object) : null;
    return {
      ok: true,
      provider: detected ?? fallback,
      credential: object,
      detected,
    };
  }

  // 整段 Cookie / `Cookie:` 头
  const cookieText = trimmed.replace(/^cookie\s*:\s*/i, "").trim();
  if (looksLikeCookie(cookieText)) {
    const detected = detect ? providerFromCookie(cookieText) : null;
    return wrapRaw(detected ?? fallback, { cookie: cookieText, token: null }, detected !== null);
  }

  // 裸 token
  if (looksLikeToken(trimmed)) {
    const detected = detect ? providerFromToken(trimmed) : null;
    return wrapRaw(detected ?? fallback, { cookie: null, token: trimmed }, detected !== null);
  }

  return {
    ok: false,
    error: "无法识别：凭证必须是合法 JSON，或粘贴整段 Cookie / DevTools「Copy as cURL」",
  };
}

/** 仅用于界面提示：从当前输入猜渠道，猜不出返回 null。 */
export function detectProvider(text: string): Provider | null {
  const parsed = parseCredentialInput(text, "codebuddy");
  return parsed.ok ? parsed.detected : null;
}