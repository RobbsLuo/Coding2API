import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AddCredentialDialog } from "../AddCredentialDialog";
import { jsonResponse, renderPage } from "@/test-utils";

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
  beforeEach(() => localStorage.clear());

  it("登录分区与 JSON 导入分区通过 Tabs 切换", async () => {
    renderDialog();
    expect(screen.getByTestId("start-login-codebuddy")).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("add-credential-tab-import"));
    expect(screen.getByTestId("import-provider")).toBeInTheDocument();
    expect(screen.getByTestId("import-submit")).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("add-credential-tab-login"));
    expect(screen.getByTestId("start-login-codebuddy")).toBeInTheDocument();
  });

  it("小浣熊在「登录渠道」页可直接粘贴 token 添加（无 OAuth 轨道）", async () => {
    const calls: Array<{ url: string; body: unknown }> = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), body: init?.body ? JSON.parse(String(init.body)) : null });
      return jsonResponse({ id: "cred_1", tos: { level: "gray", reason: "r", basis: "b" } });
    }));
    const props = renderDialog();

    // 入口在登录页（用户点「添加凭证」默认看到的那一页），不在 JSON 导入页
    expect(screen.getByTestId("add-raccoon")).toBeInTheDocument();
    // token 为空时不可提交
    expect(screen.getByTestId("add-raccoon")).toBeDisabled();

    await userEvent.click(screen.getByTestId("raccoon-token"));
    await userEvent.paste("eyJhbGciOi.payload.sig");
    await userEvent.click(screen.getByTestId("add-raccoon"));

    const posted = calls.find((call) => call.url.includes("/api/credentials"));
    expect(posted?.body).toMatchObject({
      provider: "raccoon",
      credential: { access_token: "eyJhbGciOi.payload.sig" },
    });
    expect(props.onImported).toHaveBeenCalledWith("已添加小浣熊渠道");
  });

  it("灵犀在「登录渠道」页粘贴 Cookie 即可添加（无 OAuth 轨道）", async () => {
    const calls: Array<{ url: string; body: unknown }> = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), body: init?.body ? JSON.parse(String(init.body)) : null });
      return jsonResponse({ id: "cred_2", tos: { level: "gray", reason: "r", basis: "b" } });
    }));
    const props = renderDialog();

    expect(screen.getByTestId("add-lingxi")).toBeInTheDocument();
    expect(screen.getByTestId("add-lingxi")).toBeDisabled();

    await userEvent.click(screen.getByTestId("lingxi-cookie"));
    await userEvent.paste("wps_sid=abc; uid=42");
    await userEvent.click(screen.getByTestId("add-lingxi"));

    const posted = calls.find((call) => call.url.includes("/api/credentials"));
    expect(posted?.body).toMatchObject({
      provider: "lingxi",
      credential: { cookie: "wps_sid=abc; uid=42" },
    });
    expect(props.onImported).toHaveBeenCalledWith("已添加灵犀渠道");
  });

  it("JSON 导入选择渠道后自动填入默认模板，且不覆盖已手写内容", async () => {
    renderDialog();
    await userEvent.click(screen.getByTestId("add-credential-tab-import"));
    const payload = screen.getByTestId("import-payload");

    // 下拉切到灵犀：空输入框 → 填 lingxi 模板
    await userEvent.selectOptions(screen.getByTestId("import-provider"), "lingxi");
    expect(payload).toHaveValue('{\n  "cookie": "wps_sid=…; uid=…"\n}');

    // 仍是上一渠道模板 → 跟随渠道换成新模板
    await userEvent.selectOptions(screen.getByTestId("import-provider"), "raccoon");
    expect(payload).toHaveValue('{\n  "access_token": ""\n}');

    // 已手写内容 → 切渠道不覆盖（凭据不可恢复，误清要重新取）
    await userEvent.clear(payload);
    await userEvent.type(payload, '{{"access_token":"typed"}');
    await userEvent.selectOptions(screen.getByTestId("import-provider"), "lingxi");
    expect(payload).toHaveValue('{"access_token":"typed"}');
  });

  it("粘贴 Copy as cURL 自动识别灵犀并导入（HttpOnly Cookie 也可）", async () => {
    const calls: Array<{ url: string; body: unknown }> = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), body: init?.body ? JSON.parse(String(init.body)) : null });
      return jsonResponse({ id: "cred_curl", tos: { level: "gray", reason: "r", basis: "b" } });
    }));
    const props = renderDialog();
    await userEvent.click(screen.getByTestId("add-credential-tab-import"));
    await userEvent.click(screen.getByTestId("import-payload"));
    await userEvent.paste(
      "curl 'https://lingxi.wps.cn/api' -H 'cookie: wps_sid=abc; uid=1'",
    );

    // 识别结果直接反映到渠道下拉与提示，不用回头再选
    expect(screen.getByTestId("import-detected")).toHaveTextContent("灵犀");
    expect(screen.getByTestId("import-provider")).toHaveValue("lingxi");

    await userEvent.click(screen.getByTestId("import-submit"));
    const posted = calls.find((call) => call.url.endsWith("/api/credentials"));
    expect(posted?.body).toMatchObject({
      provider: "lingxi",
      credential: { cookie: "wps_sid=abc; uid=1" },
    });
    expect(props.onImported).toHaveBeenCalledWith(expect.stringContaining("凭证已导入"));
    expect(localStorage.getItem("coding2api.importProvider")).toBe("lingxi");
  });

  it("已有灵犀凭证时提示新登录会顶掉旧会话", async () => {
    renderDialog({ lingxiCount: 1 });
    await userEvent.click(screen.getByTestId("add-credential-tab-import"));
    await userEvent.selectOptions(screen.getByTestId("import-provider"), "lingxi");

    const warning = screen.getByTestId("import-lingxi-warning");
    expect(warning).toHaveTextContent("同一浏览器只保留最近一次登录");
    expect(warning).toHaveTextContent("不同浏览器配置");

    await userEvent.selectOptions(screen.getByTestId("import-provider"), "codebuddy");
    expect(screen.queryByTestId("import-lingxi-warning")).not.toBeInTheDocument();
  });

it("记住上次成功导入的渠道", async () => {
    localStorage.setItem("coding2api.importProvider", "raccoon");
    renderDialog();
    await userEvent.click(screen.getByTestId("add-credential-tab-import"));
    expect(screen.getByTestId("import-provider")).toHaveValue("raccoon");
  });

  it("非法 JSON 本地拦截（不触发 onImported）", async () => {    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({})));
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

  it("poll 重新登录同一账号 → 提示已更新既有凭证", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/upstream/start")) {
        return jsonResponse({ flow: "poll", state: "s", auth_url: "https://a", interval: 1 });
      }
      if (url.includes("/upstream/poll")) {
        return jsonResponse({ status: "success", credential_id: "cred_old", merged: true });
      }
      return jsonResponse({ credentials: [], viewer: "root", is_admin: true });
    }));
    vi.stubGlobal("open", () => null);
    const props = renderDialog();
    await userEvent.click(screen.getByTestId("start-login-codebuddy"));
    await vi.advanceTimersByTimeAsync(2000);

    expect(props.onImported).toHaveBeenCalledWith("登录成功，已更新该账号的凭证。");
    vi.useRealTimers();
  });

  it("poll 失败时透出上游可行动原因，而不是兜底文案", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/upstream/start")) {
        return jsonResponse({ flow: "poll", state: "s", auth_url: "https://a", interval: 1 });
      }
      if (url.includes("/upstream/poll")) {
        return jsonResponse({ error: {
          code: "upstream_error",
          message: "这个账号还没开通 OfficeAce 服务。请先在 OfficeAce 客户端里登录并兑换邀请码完成开通，再回来重新登录。",
        } }, 400);
      }
      return jsonResponse({ credentials: [], viewer: "root", is_admin: true });
    }));
    vi.stubGlobal("open", () => null);
    const props = renderDialog();
    await userEvent.click(screen.getByTestId("start-login-officeace"));
    await vi.advanceTimersByTimeAsync(2000);

    expect(props.onError).toHaveBeenCalledWith(
      expect.stringContaining("还没开通 OfficeAce 服务"));
    vi.useRealTimers();
  });

  it("OfficeAce「换账号」带着 force_login 发起登录", async () => {
    const bodies: unknown[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/upstream/start")) {
        bodies.push(JSON.parse(String(init?.body ?? "")));
        return jsonResponse({ flow: "poll", state: "s", auth_url: "https://a", interval: 5 });
      }
      return jsonResponse({ credentials: [], viewer: "root", is_admin: true });
    }));
    vi.stubGlobal("open", () => ({ closed: false, close: vi.fn(), opener: {}, location: { href: "" } }));
    renderDialog();
    await userEvent.click(screen.getByTestId("switch-login-officeace"));

    expect(bodies).toEqual([{ provider: "officeace", force_login: true }]);
  });

  it("拒绝非 http(s) 授权地址，不对弹窗赋值", async () => {
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

  it("Esc 关闭对话框（无登录进行中时）", async () => {
    const props = renderDialog();
    expect(screen.getByTestId("add-credential-dialog")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(props.onClose).toHaveBeenCalledTimes(1);
  });
});

describe("ToS 风险提示", () => {
  it("选择渠道后展示评级与理由（灰区用 warn 语气）", async () => {
    renderDialog({
      tosRisks: {
        codebuddy: { level: "gray", reason: "复用客户端私有协议", basis: "docs/x.md" },
        codebuddy_intl: { level: "gray", reason: "独立账号体系", basis: "运营判断" },
      },
    });
    await userEvent.click(screen.getByTestId("add-credential-tab-import"));
    expect(screen.getByTestId("import-tos-risk")).toHaveTextContent("gray");
    expect(screen.getByTestId("import-tos-risk")).toHaveTextContent("复用客户端私有协议");
    // 依据不是「运营判断」时补上出处
    expect(screen.getByTestId("import-tos-risk")).toHaveTextContent("docs/x.md");
    await userEvent.selectOptions(screen.getByTestId("import-provider"), "codebuddy_intl");
    expect(screen.getByTestId("import-tos-risk")).toHaveTextContent("独立账号体系");
    expect(screen.getByTestId("import-tos-risk")).not.toHaveTextContent("依据");
  });

  it("导入成功时把灰区评级带进提示", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      id: "c1",
      tos: { level: "gray", reason: "条款未明确允许", basis: "运营判断" },
    })));
    const props = renderDialog();
    await userEvent.click(screen.getByTestId("add-credential-tab-import"));
    await userEvent.click(screen.getByTestId("import-payload"));
    await userEvent.paste('{"token":"x"}');
    await userEvent.click(screen.getByTestId("import-submit"));

    expect(props.onImported).toHaveBeenCalledWith(
      expect.stringContaining("ToS 评级为 gray"),
    );
  });
});
