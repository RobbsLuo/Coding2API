import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { CredentialLoginSecretDialog } from "../CredentialLoginSecretDialog";
import { api } from "../../api/client";
import { makeCredential } from "../../test-utils";
import type { Credential } from "../../api/types";

vi.mock("../../api/client", () => ({
  api: {
    setCredentialLoginSecret: vi.fn(),
  },
}));

const setCredentialLoginSecret = vi.mocked(api.setCredentialLoginSecret);

function renderDialog(overrides: Record<string, unknown> = {}) {
  const onSaved = vi.fn();
  const onError = vi.fn();
  render(
    <CredentialLoginSecretDialog
      credential={makeCredential({ provider: "raccoon", ...overrides }) as Credential}
      onClose={vi.fn()}
      onSaved={onSaved}
      onError={onError}
    />,
  );
  return { onSaved, onError };
}

describe("CredentialLoginSecretDialog", () => {
  it("保存手机号与密码并提示按需登录", async () => {
    setCredentialLoginSecret.mockResolvedValue({ ok: true, login_secret: 1 });
    const { onSaved } = renderDialog();

    await userEvent.type(
      screen.getByTestId("credential-login-phone-input"), "13900000001");
    await userEvent.type(
      screen.getByTestId("credential-login-password-input"), "Abcd123456");
    await userEvent.click(screen.getByTestId("credential-login-secret-save"));

    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(setCredentialLoginSecret).toHaveBeenCalledWith(
      expect.any(String), "13900000001", "Abcd123456");
    expect(onSaved.mock.calls[0][0]).toContain("按需登录");
  });

  it("两个都留空 = 清除", async () => {
    setCredentialLoginSecret.mockResolvedValue({ ok: true, login_secret: 0 });
    const { onSaved } = renderDialog({ login_secret: 1 });

    await userEvent.click(screen.getByTestId("credential-login-secret-save"));

    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(setCredentialLoginSecret).toHaveBeenCalledWith(
      expect.any(String), "", "");
    expect(onSaved.mock.calls[0][0]).toContain("已清除");
  });

  it("只填一半在本地就拦住，不发请求", async () => {
    const { onSaved } = renderDialog();
    await userEvent.type(
      screen.getByTestId("credential-login-phone-input"), "13900000001");
    await userEvent.click(screen.getByTestId("credential-login-secret-save"));

    expect(screen.getByTestId("credential-login-secret-error")).toHaveTextContent(
      "同时填写");
    expect(setCredentialLoginSecret).not.toHaveBeenCalled();
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("已保存时显示标记，且输入框始终为空（密码不回显）", () => {
    renderDialog({ login_secret: 1 });
    expect(screen.getByText("（已保存）")).toBeInTheDocument();
    expect(screen.getByTestId("credential-login-phone-input")).toHaveValue("");
    expect(screen.getByTestId("credential-login-password-input")).toHaveValue("");
  });

  it("接口失败走 onError", async () => {
    setCredentialLoginSecret.mockRejectedValue(new Error("boom"));
    const { onError } = renderDialog();
    await userEvent.type(
      screen.getByTestId("credential-login-phone-input"), "139");
    await userEvent.type(
      screen.getByTestId("credential-login-password-input"), "Abcd123456");
    await userEvent.click(screen.getByTestId("credential-login-secret-save"));

    await waitFor(() => expect(onError).toHaveBeenCalledWith("boom"));
  });
});