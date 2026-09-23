import { screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ActivatePage } from "../ActivatePage";
import { ChangePasswordDialog } from "../../components/ChangePasswordDialog";
import { jsonResponse, renderStandalone, userEvent } from "./helpers";
import { Routes, Route } from "react-router-dom";

/** 让 useSearchParams 拿到 token：包一层带查询串的路由。 */
function renderActivate(search: string) {
  return renderStandalone(
    <Routes>
      <Route path="/activate" element={<ActivatePage />} />
    </Routes>,
    search ? `/activate?${search}` : "/activate",
  );
}

describe("ActivatePage", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("无 token 时直接判为无效链接", async () => {
    renderActivate("");
    expect(await screen.findByTestId("activate-invalid")).toBeInTheDocument();
  });

  it("token 校验失败判为无效", async () => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      jsonResponse({ error: { code: "invalid_request", message: "invalid or expired activation token" } }, 400)));
    renderActivate("token=bad");
    expect(await screen.findByTestId("activate-invalid")).toBeInTheDocument();
  });

  it("有效 token 显示用户名，两次不一致时报错", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ username: "alice", valid: true })));
    renderActivate("token=good");
    expect(await screen.findByText("alice")).toBeInTheDocument();

    await userEvent.type(screen.getByTestId("activate-password"), "longenough");
    await userEvent.type(screen.getByTestId("activate-confirm"), "different");
    await userEvent.click(screen.getByRole("button", { name: "设置密码" }));
    expect(await screen.findByTestId("activate-error")).toHaveTextContent("不一致");
  });

  it("密码过短时报错", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ username: "alice", valid: true })));
    renderActivate("token=good");
    await screen.findByText("alice");
    await userEvent.type(screen.getByTestId("activate-password"), "short");
    await userEvent.type(screen.getByTestId("activate-confirm"), "short");
    await userEvent.click(screen.getByRole("button", { name: "设置密码" }));
    expect(await screen.findByTestId("activate-error")).toHaveTextContent("至少 8 位");
  });

  it("激活成功后可前往登录", async () => {
    vi.stubGlobal("location", { href: "" });
    vi.stubGlobal("fetch", vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "POST") return jsonResponse({ ok: true, username: "alice" });
      return jsonResponse({ username: "alice", valid: true });
    }));
    renderActivate("token=good");
    await screen.findByText("alice");
    await userEvent.type(screen.getByTestId("activate-password"), "longenough");
    await userEvent.type(screen.getByTestId("activate-confirm"), "longenough");
    await userEvent.click(screen.getByRole("button", { name: "设置密码" }));

    expect(await screen.findByTestId("activate-done")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "前往登录" }));
    expect(window.location.href).toBe("/login");
  });

  it("激活失败（令牌过期）给出索取新链接的提示", async () => {
    vi.stubGlobal("fetch", vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "POST") {
        return jsonResponse({ error: { code: "invalid_request", message: "invalid or expired activation token" } }, 400);
      }
      return jsonResponse({ username: "alice", valid: true });
    }));
    renderActivate("token=good");
    await screen.findByText("alice");
    await userEvent.type(screen.getByTestId("activate-password"), "longenough");
    await userEvent.type(screen.getByTestId("activate-confirm"), "longenough");
    await userEvent.click(screen.getByRole("button", { name: "设置密码" }));
    await waitFor(() =>
      expect(screen.getByTestId("activate-error")).toHaveTextContent("链接无效或已过期"));
  });
});

describe("ChangePasswordDialog", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("强制模式下没有取消按钮", () => {
    renderStandalone(<ChangePasswordDialog required />);
    expect(screen.getByTestId("change-password-dialog")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "取消" })).not.toBeInTheDocument();
    expect(screen.getByText(/必须先设置新密码/)).toBeInTheDocument();
  });

  it("自愿模式下可取消", async () => {
    const onCancel = vi.fn();
    renderStandalone(<ChangePasswordDialog onCancel={onCancel} />);
    await userEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(onCancel).toHaveBeenCalled();
  });

  it("新密码不一致时报错、不发请求", async () => {
    const spy = vi.fn();
    vi.stubGlobal("fetch", spy);
    renderStandalone(<ChangePasswordDialog />);
    await userEvent.type(screen.getByTestId("password-current"), "oldpw123");
    await userEvent.type(screen.getByTestId("password-new"), "newpw1234");
    await userEvent.type(screen.getByTestId("password-confirm"), "mismatch");
    await userEvent.click(screen.getByRole("button", { name: "确认修改" }));
    expect(await screen.findByTestId("password-error")).toHaveTextContent("不一致");
    expect(spy).not.toHaveBeenCalled();
  });

  it("当前密码错误时提示", async () => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      jsonResponse({ error: { code: "invalid_request", message: "current password is incorrect" } }, 400)));
    renderStandalone(<ChangePasswordDialog />);
    await userEvent.type(screen.getByTestId("password-current"), "wrongpw1");
    await userEvent.type(screen.getByTestId("password-new"), "newpw1234");
    await userEvent.type(screen.getByTestId("password-confirm"), "newpw1234");
    await userEvent.click(screen.getByRole("button", { name: "确认修改" }));
    expect(await screen.findByTestId("password-error")).toHaveTextContent("当前密码不正确");
  });

  it("成功后调用 onDone", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ ok: true })));
    const onDone = vi.fn();
    renderStandalone(<ChangePasswordDialog onDone={onDone} />);
    await userEvent.type(screen.getByTestId("password-current"), "oldpw123");
    await userEvent.type(screen.getByTestId("password-new"), "newpw1234");
    await userEvent.type(screen.getByTestId("password-confirm"), "newpw1234");
    await userEvent.click(screen.getByRole("button", { name: "确认修改" }));
    await waitFor(() => expect(onDone).toHaveBeenCalled());
  });

  it("成功后无 onDone 时回首页（换发 Cookie 后重取会话）", async () => {
    vi.stubGlobal("location", { href: "" });
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ ok: true })));
    renderStandalone(<ChangePasswordDialog />);
    await userEvent.type(screen.getByTestId("password-current"), "oldpw123");
    await userEvent.type(screen.getByTestId("password-new"), "newpw1234");
    await userEvent.type(screen.getByTestId("password-confirm"), "newpw1234");
    await userEvent.click(screen.getByRole("button", { name: "确认修改" }));
    await waitFor(() => expect(window.location.href).toBe("/"));
  });
});
