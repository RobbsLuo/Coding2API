import { describe, expect, it } from "vitest";
import {
  detectProvider,
  headersFromCurl,
  parseCredentialInput,
} from "../credentialImport";

/** 造一个只用于识别 `iss` 的未签名 JWT。 */
function jwt(payload: Record<string, unknown>): string {
  const encode = (value: unknown) =>
    btoa(JSON.stringify(value)).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
  return `${encode({ alg: "none" })}.${encode(payload)}.sig`;
}

describe("parseCredentialInput", () => {
  it("渠道 JSON 原样透传，选中渠道没有识别特征时就用它", () => {
    expect(parseCredentialInput('{"token":"abc"}', "codebuddy")).toEqual({
      ok: true,
      provider: "codebuddy",
      credential: { token: "abc" },
      detected: null,
    });
  });

  it("带 provider 的批量导入同形对象会自动拆包", () => {
    const parsed = parseCredentialInput(
      '{"provider":"lingxi","credential":{"cookie":"wps_sid=abc"}}',
      "codebuddy",
    );
    expect(parsed).toEqual({
      ok: true,
      provider: "lingxi",
      credential: { cookie: "wps_sid=abc" },
      detected: "lingxi",
    });
  });

  it("JSON 数组提示走批量导入", () => {
    const parsed = parseCredentialInput('[{"provider":"lingxi"}]', "codebuddy");
    expect(parsed.ok).toBe(false);
    if (!parsed.ok) expect(parsed.error).toContain("批量导入");
  });

  it("TRAE 凭证按字段名识别，即使下拉停在别的渠道", () => {
    const parsed = parseCredentialInput(
      '{"accessToken":"a","uid":"u","refreshToken":"r"}',
      "codebuddy",
    );
    expect(parsed.ok && parsed.provider).toBe("trae");
  });

  it("整段 Cookie 按 wps_sid 自动识别为灵犀", () => {
    expect(parseCredentialInput("wps_sid=abc; uid=42", "codebuddy")).toEqual({
      ok: true,
      provider: "lingxi",
      credential: { cookie: "wps_sid=abc; uid=42" },
      detected: "lingxi",
    });
  });

  it("Cookie: 前缀会被剥掉", () => {
    const parsed = parseCredentialInput("Cookie: xhx_token=abc; path=/", "codebuddy");
    expect(parsed).toEqual({
      ok: true,
      provider: "raccoon",
      credential: { cookie: "xhx_token=abc; path=/" },
      detected: "raccoon",
    });
  });

  it("JWT 的 iss 指向小浣熊时识别为小浣熊", () => {
    const token = jwt({ iss: "xiaohuanxiong.com/iam" });
    expect(parseCredentialInput(token, "codebuddy")).toEqual({
      ok: true,
      provider: "raccoon",
      credential: { access_token: token },
      detected: "raccoon",
    });
  });

  it("裸 token 无识别特征时落到选中渠道", () => {
    const parsed = parseCredentialInput("abcdefghijklmnopqrstuvwxyz", "qoder");
    expect(parsed).toEqual({
      ok: true,
      provider: "qoder",
      credential: { token: "abcdefghijklmnopqrstuvwxyz" },
      detected: null,
    });
  });

  it("裸 token 给 TRAE 时明确拒绝，不落半截凭证", () => {
    const parsed = parseCredentialInput("abcdefghijklmnopqrstuvwxyz", "trae");
    expect(parsed.ok).toBe(false);
    if (!parsed.ok) expect(parsed.error).toContain("完整 JSON");
  });

  it("Cookie 给不支持 Cookie 的渠道时明确拒绝", () => {
    const parsed = parseCredentialInput("wps_sid=abc; x=1", "codebuddy", false);
    expect(parsed.ok).toBe(false);
    if (!parsed.ok) expect(parsed.error).toContain("不使用 Cookie");
  });

  it("detect=false 时不猜渠道，按传入渠道包装", () => {
    const parsed = parseCredentialInput("wps_sid=abc; uid=1", "codebuddy", false);
    expect(parsed.ok).toBe(false);
    expect(detectProvider("wps_sid=abc; uid=1")).toBe("lingxi");
  });

  it("空输入与无法识别的文本都报错", () => {
    expect(parseCredentialInput("   ", "codebuddy").ok).toBe(false);
    const parsed = parseCredentialInput("not-json", "codebuddy");
    expect(parsed.ok).toBe(false);
    if (!parsed.ok) expect(parsed.error).toContain("必须是合法 JSON");
  });
});

describe("headersFromCurl", () => {
  it("解析 -H 的 Cookie / Authorization（大小写不敏感）", () => {
    const curl = [
      "curl 'https://lingxi.wps.cn/api' \\",
      "  -H 'accept: application/json' \\",
      "  -H 'cookie: wps_sid=abc; uid=1' \\",
      "  -H 'Authorization: Bearer tok'",
    ].join("\n");
    expect(headersFromCurl(curl)).toEqual({
      cookie: "wps_sid=abc; uid=1",
      authorization: "Bearer tok",
    });
  });

  it("支持 -b 传整段 Cookie", () => {
    expect(headersFromCurl("curl -b 'xhx_token=abc; path=/' https://x")).toEqual({
      cookie: "xhx_token=abc; path=/",
      authorization: null,
    });
  });

  it("从 cURL 提取 Cookie 后自动识别渠道", () => {
    const parsed = parseCredentialInput(
      "curl 'https://lingxi.wps.cn/x' -H 'cookie: wps_sid=abc; uid=1'",
      "codebuddy",
    );
    expect(parsed).toEqual({
      ok: true,
      provider: "lingxi",
      credential: { cookie: "wps_sid=abc; uid=1" },
      detected: "lingxi",
    });
  });

  it("从 cURL 的 Bearer 里识别小浣熊", () => {
    const token = jwt({ iss: "xiaohuanxiong.com/iam" });
    const parsed = parseCredentialInput(
      `curl 'https://x' -H 'authorization: Bearer ${token}'`,
      "codebuddy",
    );
    expect(parsed.ok && parsed.provider).toBe("raccoon");
    expect(parsed.ok && parsed.credential).toEqual({ access_token: token });
  });

  it("cURL 没有登录头时给出可操作错误", () => {
    const parsed = parseCredentialInput(
      "curl 'https://x' -H 'accept: application/json'",
      "codebuddy",
    );
    expect(parsed.ok).toBe(false);
    if (!parsed.ok) expect(parsed.error).toContain("没有 Cookie 或 Authorization");
  });
});