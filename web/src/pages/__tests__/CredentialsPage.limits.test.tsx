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

async function openLimits() {
  await userEvent.click(screen.getByTestId("actions-cred_1"));
  await userEvent.click(screen.getByRole("menuitem", { name: "设置限额/模型" }));
  return screen.findByTestId("credential-limits-dialog");
}

describe("CredentialsPage 限额护栏", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("未配置限额时菜单显示「设置限额/模型」，保存后调用 PUT", async () => {
    const calls: { url: string; method?: string; body?: unknown }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/limits")) {
        calls.push({ url, method: init?.method, body: JSON.parse(String(init?.body)) });
        return jsonResponse({ limits: { daily_tokens: 1000, per_model_tokens: {},
                                        keep_credits: null, daily_credits: null,
                                        updated_at: 1, updated_by: "root" } });
      }
      return jsonResponse(listBody([makeCredential({ id: "cred_1" })]));
    }));

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    const dialog = await openLimits();

    await userEvent.type(screen.getByTestId("limits-daily-tokens"), "1000");
    await userEvent.type(screen.getByTestId("limits-per-model"), "glm-5=500000");
    await userEvent.click(screen.getByTestId("limits-save"));

    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].url).toBe("/api/credentials/cred_1/limits");
    expect(calls[0].method).toBe("PUT");
    expect(calls[0].body).toEqual({
      keep_credits: null,
      daily_tokens: 1000,
      daily_credits: null,
      per_model_tokens: { "glm-5": 500000 },
      // v39：账号级模型白名单（'' 清空/不限制）+ 乐观并发版本号
      allowed_models: "",
      expected_updated_at: 0,
    });
    // 成功后关闭对话框并播报（对话框由父组件按 id 卸载）
    await waitFor(() => expect(dialog).not.toBeInTheDocument());
    expect(await screen.findByTestId("credentials-notice")).toHaveTextContent("限额已保存");
  });

  it("已配置限额时预填并显示徽章", async () => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      jsonResponse(listBody([
        makeCredential({
          id: "cred_1",
          limits: { keep_credits: 50, daily_tokens: 1000, daily_credits: null,
                    per_model_tokens: { "glm-5": 200 }, updated_at: 1, updated_by: "root" },
        }),
      ])),
    ));

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();

    expect(screen.getByTestId("limits-badge-cred_1")).toHaveTextContent("限额");
    await userEvent.click(screen.getByTestId("actions-cred_1"));
    await userEvent.click(screen.getByRole("menuitem", { name: "编辑限额/模型" }));
    await screen.findByTestId("credential-limits-dialog");

    expect(screen.getByTestId("limits-keep-credits")).toHaveValue(50);
    expect(screen.getByTestId("limits-daily-tokens")).toHaveValue(1000);
    expect(screen.getByTestId("limits-per-model")).toHaveValue("glm-5=200");
  });

  it("保存失败时显示错误、对话框保持打开", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes("/limits")) {
        return jsonResponse(
          { error: { message: "daily_tokens must be a number", code: "invalid_request", status: 400 } },
          400,
        );
      }
      return jsonResponse(listBody([makeCredential({ id: "cred_1" })]));
    }));

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    await openLimits();
    await userEvent.click(screen.getByTestId("limits-save"));

    expect(await screen.findByTestId("credentials-error")).toHaveTextContent("daily_tokens");
    expect(screen.getByTestId("credential-limits-dialog")).toBeInTheDocument();
  });

  it("取消关闭对话框且不发请求", async () => {
    const fetchSpy = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        jsonResponse(listBody([makeCredential({ id: "cred_1" })])),
    );
    vi.stubGlobal("fetch", fetchSpy);

    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    await openLimits();
    await userEvent.click(screen.getByRole("button", { name: "取消" }));

    await waitFor(() =>
      expect(screen.queryByTestId("credential-limits-dialog")).not.toBeInTheDocument(),
    );
    expect(fetchSpy.mock.calls.every(([input]) => !String(input).includes("/limits"))).toBe(true);
  });
});
