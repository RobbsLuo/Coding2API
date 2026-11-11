import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { api } from "../../api/client";
import { makeCredential } from "../../test-utils";
import {
  CredentialWebCookieDialog,
  MAX_WEB_COOKIE_LENGTH,
} from "../CredentialWebCookieDialog";

const credential = makeCredential({ id: "cred_q", provider: "qoder", nickname: "Q" });

function setup(overrides: Partial<Parameters<typeof CredentialWebCookieDialog>[0]> = {}) {
  const onSaved = vi.fn();
  const onError = vi.fn();
  render(
    <CredentialWebCookieDialog
      credential={credential as never}
      onClose={vi.fn()}
      onSaved={onSaved}
      onError={onError}
      {...overrides}
    />,
  );
  return { onSaved, onError };
}

describe("CredentialWebCookieDialog", () => {
  it("保存粘贴的 Cookie（trim 后提交）并回调成功文案", async () => {
    const saved = vi.spyOn(api, "setCredentialWebCookie").mockResolvedValue({ ok: true });
    const { onSaved } = setup();

    await userEvent.type(screen.getByTestId("credential-web-cookie-input"),
                         "  qoder_session_cookie=abc  ");
    await userEvent.click(screen.getByTestId("credential-web-cookie-save"));

    await waitFor(() => expect(saved).toHaveBeenCalledWith("cred_q",
                                                           "qoder_session_cookie=abc"));
    expect(onSaved).toHaveBeenCalledWith(expect.stringContaining("已保存"));
  });

  it("留空提交 = 清除 Cookie", async () => {
    const saved = vi.spyOn(api, "setCredentialWebCookie").mockResolvedValue({ ok: true });
    const { onSaved } = setup();

    await userEvent.click(screen.getByTestId("credential-web-cookie-save"));

    await waitFor(() => expect(saved).toHaveBeenCalledWith("cred_q", ""));
    expect(onSaved).toHaveBeenCalledWith(expect.stringContaining("已清除"));
  });

  it("超长输入本地拦截，不打上游", async () => {
    const saved = vi.spyOn(api, "setCredentialWebCookie");
    setup();

    await userEvent.click(screen.getByTestId("credential-web-cookie-input"));
    await userEvent.paste("x".repeat(MAX_WEB_COOKIE_LENGTH + 1));
    await userEvent.click(screen.getByTestId("credential-web-cookie-save"));

    expect(screen.getByTestId("credential-web-cookie-error")).toHaveTextContent("过长");
    expect(saved).not.toHaveBeenCalled();
  });

  it("后端报错走 onError，不误报成功", async () => {
    vi.spyOn(api, "setCredentialWebCookie").mockRejectedValue(new Error("boom"));
    const { onSaved, onError } = setup();

    await userEvent.type(screen.getByTestId("credential-web-cookie-input"), "sid=1");
    await userEvent.click(screen.getByTestId("credential-web-cookie-save"));

    await waitFor(() => expect(onError).toHaveBeenCalledWith("boom"));
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("不回显已有 Cookie（落库值不可读）", () => {
    setup();
    expect(screen.getByTestId("credential-web-cookie-input")).toHaveValue("");
  });

  it("CodeArts 使用控制台明细接口的提示与占位符", () => {
    setup({
      credential: makeCredential({
        id: "cred_ca", provider: "codearts", nickname: "CA",
      }) as never,
    });
    expect(screen.getByText(/credit\/overview/)).toBeInTheDocument();
    expect(screen.getByTestId("credential-web-cookie-input")).toHaveAttribute(
      "placeholder", expect.stringContaining("devclouddevuibjJ_SESSION_ID"),
    );
  });
});