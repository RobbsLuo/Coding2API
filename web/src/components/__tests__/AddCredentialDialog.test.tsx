import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { AddCredentialDialog } from "../AddCredentialDialog";
import { jsonResponse, renderPage } from "../../pages/__tests__/helpers";

function renderDialog(overrides: Partial<Parameters<typeof AddCredentialDialog>[0]> = {}) {
  const props = {
    open: true,
    onClose: vi.fn(),
    credentialCount: 0,
    hasZen: false,
    hasKilo: false,
    onImported: vi.fn(),
    onNotice: vi.fn(),
    onError: vi.fn(),
    ...overrides,
  };
  renderPage(<AddCredentialDialog {...props} />);
  return props;
}

describe("AddCredentialDialog", () => {
  it("登录分区与 JSON 导入分区通过 Tabs 切换", async () => {
    renderDialog();
    expect(screen.getByTestId("start-login-codebuddy")).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("add-credential-tab-import"));
    expect(screen.getByTestId("import-provider")).toBeInTheDocument();
    expect(screen.getByTestId("import-submit")).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("add-credential-tab-login"));
    expect(screen.getByTestId("start-login-codebuddy")).toBeInTheDocument();
  });

  it("非法 JSON 本地拦截（不触发 onImported）", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({})));
    renderDialog();
    await userEvent.click(screen.getByTestId("add-credential-tab-import"));
    await userEvent.click(screen.getByTestId("import-payload"));
    await userEvent.paste("not-json");
    await userEvent.click(screen.getByTestId("import-submit"));

    expect(screen.getByTestId("import-error")).toHaveTextContent("必须是合法 JSON");
  });

  it("TRAE 登录中禁止关闭对话框（授权完成检测依赖列表轮询）", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/upstream/start")) {
        return jsonResponse({
          flow: "callback", state: "m:d", auth_url: "https://trae.example/x",
          interval: null, callback_url: "http://127.0.0.1:8000/authorize",
        });
      }
      return jsonResponse({ credentials: [], viewer: "root", is_admin: true });
    }));
    vi.stubGlobal("open", () => null);
    const props = renderDialog();
    await userEvent.click(screen.getByTestId("start-login-trae"));
    await screen.findByTestId("cancel-login-trae");

    // 点遮罩与关闭按钮都不会关
    await userEvent.click(screen.getByTestId("add-credential-close"));
    expect(props.onClose).not.toHaveBeenCalled();
  });

  it("poll 登录成功后回调 onImported（关对话框 + 刷新列表）", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/upstream/start")) {
        return jsonResponse({ flow: "poll", state: "s", auth_url: "https://a", interval: 1 });
      }
      if (url.includes("/upstream/poll")) {
        return jsonResponse({ status: "success", credential_id: "cred_new" });
      }
      return jsonResponse({ credentials: [], viewer: "root", is_admin: true });
    }));
    vi.stubGlobal("open", () => null);
    const props = renderDialog();
    await userEvent.click(screen.getByTestId("start-login-codebuddy"));
    await vi.advanceTimersByTimeAsync(2000);

    expect(props.onImported).toHaveBeenCalledWith("登录成功，凭证已保存。");
    vi.useRealTimers();
  });

  it("拒绝非 http(s) 授权地址，不对弹窗赋值（M5）", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/upstream/start")) {
        return jsonResponse({ flow: "poll", state: "s",
                              auth_url: "javascript:alert(1)", interval: 5 });
      }
      return jsonResponse({ credentials: [], viewer: "root", is_admin: true });
    }));
    const popup = { closed: false, close: vi.fn(), opener: {}, location: { href: "" } };
    vi.stubGlobal("open", () => popup);
    const props = renderDialog();
    await userEvent.click(screen.getByTestId("start-login-codebuddy"));

    expect(popup.location.href).toBe("");        // 恶意 scheme 绝不赋值
    expect(popup.close).toHaveBeenCalled();
    expect(popup.opener).toBeNull();             // opener 已切断
    expect(props.onError).toHaveBeenCalledWith("授权地址无效，已中止登录。");
  });
});
