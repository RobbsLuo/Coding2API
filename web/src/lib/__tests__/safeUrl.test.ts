import { describe, expect, it } from "vitest";
import { safeHttpUrl } from "../safeUrl";

/**
 * 外链协议白名单。
 *
 * 上游下发的 URL（`incident.url`、授权 `authUrl`）会直接进 `href`。
 * `javascript:` / `data:` 赋给 href 会在点击时执行脚本（`data:` 还能伪造
 * 整页），所以凡是把外部数据变成链接的地方都必须过这个闸。
 */
describe("safeHttpUrl", () => {
  it("放行 http(s)", () => {
    expect(safeHttpUrl("https://status.openai.com/incidents/1"))
      .toBe("https://status.openai.com/incidents/1");
    expect(safeHttpUrl("http://example.com/x")).toBe("http://example.com/x");
  });

  it("拒绝 javascript: / data: / vbscript: 等脚本协议", () => {
    expect(safeHttpUrl("javascript:alert(1)")).toBeNull();
    expect(safeHttpUrl("data:text/html,<script>alert(1)</script>")).toBeNull();
    expect(safeHttpUrl("vbscript:msgbox(1)")).toBeNull();
  });

  it("拒绝空值与非法 URL", () => {
    expect(safeHttpUrl(undefined)).toBeNull();
    expect(safeHttpUrl(null)).toBeNull();
    expect(safeHttpUrl("")).toBeNull();
    expect(safeHttpUrl("not a url")).toBeNull();
  });

  it("协议大小写不敏感（JAVASCRIPT: 也是脚本协议）", () => {
    expect(safeHttpUrl("JavaScript:alert(1)")).toBeNull();
    expect(safeHttpUrl("JAVASCRIPT:alert(1)")).toBeNull();
  });
});
