import { screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CredentialsPage } from "../CredentialsPage";
import { expandCredentialGroups, jsonResponse, makeCredential, renderPage, settle, userEvent } from "@/test-utils";

const ADMIN = { username: "root", is_admin: true } as const;

function listBody(credentials: unknown[]) {
  return {
    credentials,
    expiry_window_seconds: 129600,
    expiry_secondary_window_seconds: 604800,
    token_expiry_warning_seconds: 3600,
    viewer: "root",
    is_admin: true,
    is_operator: true,
  };
}

async function openLimits(name = "设置限额/模型") {
  await userEvent.click(screen.getByTestId("actions-cred_1"));
  await userEvent.click(screen.getByRole("menuitem", { name }));
  return screen.findByTestId("credential-limits-dialog");
}

describe("CredentialsPage 模型白名单与编辑冲突", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("提交时带 allowed_models 与 expected_updated_at", async () => {
    const calls: { url: string; method?: string; body?: Record<string, unknown> }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/limits")) {
        calls.push({ url, method: init?.method, body: JSON.parse(String(init?.body)) });
        return jsonResponse({
          limits: null, allowed_models: "glm-*", updated_at: 101,
        });
      }
      return jsonResponse(listBody([
        makeCredential({ id: "cred_1", updated_at: 100, allowed_models: "" }),
      ]));
    }));

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    await openLimits();

    await userEvent.type(screen.getByTestId("limits-allowed-models"), "glm-*");
    await userEvent.click(screen.getByTestId("limits-save"));

    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].url).toBe("/api/credentials/cred_1/limits");
    expect(calls[0].method).toBe("PUT");
    // 版本号来自打开时的列表快照，不是提交时的（否则永远不冲突）
    expect(calls[0].body).toMatchObject({ allowed_models: "glm-*", expected_updated_at: 100 });
    await waitFor(() =>
      expect(screen.queryByTestId("credential-limits-dialog")).not.toBeInTheDocument());
    expect(await screen.findByTestId("credentials-notice")).toHaveTextContent("限额已保存");
  });

  it("已配置的白名单会预填、显示徽章并改菜单文案", async () => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      jsonResponse(listBody([
        makeCredential({ id: "cred_1", updated_at: 5, allowed_models: "glm-*,kimi-k3" }),
      ])),
    ));

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    expect(screen.getByTestId("models-badge-cred_1")).toHaveTextContent("限模型");
    await openLimits("编辑限额/模型");
    expect(screen.getByTestId("limits-allowed-models")).toHaveValue("glm-*,kimi-k3");
  });

  it("409 冲突时提示「已被他人修改」并关闭编辑器，不重试", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/limits")) {
        calls.push(url);
        return jsonResponse(
          {
            error: {
              message: "凭证已被他人修改，请刷新后重试",
              code: "credential_conflict",
              status: 409,
            },
            current: { id: "cred_1", updated_at: 999, allowed_models: "kimi-*", limits: null },
          },
          409,
        );
      }
      return jsonResponse(listBody([
        makeCredential({ id: "cred_1", updated_at: 100 }),
      ]));
    }));

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    const dialog = await openLimits();
    await userEvent.click(screen.getByTestId("limits-save"));

    expect(await screen.findByTestId("credentials-error")).toHaveTextContent("已被他人修改");
    await waitFor(() => expect(dialog).not.toBeInTheDocument());
    // 只发一次：冲突不自动重试（重试等于用新版本号强制覆盖别人的修改）
    expect(calls).toHaveLength(1);
  });

  it("其它错误（400）保持编辑器打开并显示后端文案", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes("/limits")) {
        return jsonResponse(
          { error: { message: "allowed_models 含过长的模型名", code: "invalid_request", status: 400 } },
          400,
        );
      }
      return jsonResponse(listBody([makeCredential({ id: "cred_1", updated_at: 1 })]));
    }));

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    await openLimits();
    await userEvent.click(screen.getByTestId("limits-save"));

    expect(await screen.findByTestId("credentials-error")).toHaveTextContent("allowed_models");
    expect(screen.getByTestId("credential-limits-dialog")).toBeInTheDocument();
  });
});
