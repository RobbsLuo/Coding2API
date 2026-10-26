/**
 * 外链协议白名单：只允许 http(s)。
 *
 * 上游下发的 URL（`incident.url`、授权 `authUrl` 等）会直接进 `href`。
 * `javascript:` / `data:` 这类 scheme 赋给 href 会在点击时执行脚本
 * （`data:` 还能伪造整页），所以凡是把外部数据变成链接的地方都必须过这里。
 *
 * 不做主机白名单：这些 URL 由各上游下发（登录域名随账号区域变化），写死
 * 域名会在上游变更时直接打断功能；真实威胁是脚本执行，scheme 校验已挡住。
 */

export function safeHttpUrl(raw: string | undefined | null): string | null {
  if (!raw) return null;
  try {
    const url = new URL(raw);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}
