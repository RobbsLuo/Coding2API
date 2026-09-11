import { screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LoginPage } from "../LoginPage";
import { jsonResponse, renderStandalone, userEvent } from "./helpers";

describe("LoginPage", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("渲染用户名与密码输入框", () => {
    renderStandalone(<LoginPage />);
    expect(screen.getByTestId("login-username")).toBeInTheDocument();
    expect(screen.getByTestId("login-password")).toBeInTheDocument();
  });

  it("提交成功后跳转首页", async () => {
    const fetchSpy = vi.fn(async () =>
      jsonResponse({ username: "root", is_admin: true }),
    );
    vi.stubGlobal("fetch", fetchSpy);
    const assign = vi.fn();
    vi.stubGlobal("location", { href: "/", assign });

    renderStandalone(<LoginPage />);
    await userEvent.type(screen.getByTestId("login-username"), "root");
    await userEvent.type(screen.getByTestId("login-password"), "pw");
    await userEvent.click(screen.getByRole("button", { name: "登录" }));

    await waitFor(() => expect(window.location.href).toBe("/"));
    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/auth/login",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("凭据错误时展示提示", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({ error: { code: "invalid_credentials", message: "bad" } }, 401),
      ),
    );
    renderStandalone(<LoginPage />);
    await userEvent.type(screen.getByTestId("login-username"), "root");
    await userEvent.type(screen.getByTestId("login-password"), "wrong");
    await userEvent.click(screen.getByRole("button", { name: "登录" }));

    expect(await screen.findByTestId("login-error")).toHaveTextContent("用户名或密码错误");
  });

  it("非 401 失败展示通用错误", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ error: { message: "boom" } }, 500)));
    renderStandalone(<LoginPage />);
    await userEvent.type(screen.getByTestId("login-username"), "root");
    await userEvent.type(screen.getByTestId("login-password"), "pw");
    await userEvent.click(screen.getByRole("button", { name: "登录" }));
    expect(await screen.findByTestId("login-error")).toHaveTextContent("登录失败");
  });

  it("提交中禁用按钮，失败后恢复可用", async () => {
    let release: (() => void) | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            // 失败路径：组件不会跳转，按钮必须恢复可用
            release = () =>
              resolve(jsonResponse({ error: { code: "invalid_credentials" } }, 401));
          }),
      ),
    );
    renderStandalone(<LoginPage />);
    await userEvent.type(screen.getByTestId("login-username"), "root");
    await userEvent.type(screen.getByTestId("login-password"), "pw");
    await userEvent.click(screen.getByRole("button", { name: "登录" }));

    expect(screen.getByRole("button", { name: "登录中…" })).toBeDisabled();
    release?.();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "登录" })).not.toBeDisabled(),
    );
  });

  it("成功提交后按钮保持禁用（页面即将跳转，不允许再次提交）", async () => {
    let release: (() => void) | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            release = () => resolve(jsonResponse({ username: "root", is_admin: true }));
          }),
      ),
    );
    const assign = vi.fn();
    vi.stubGlobal("location", { href: "/", assign });

    renderStandalone(<LoginPage />);
    await userEvent.type(screen.getByTestId("login-username"), "root");
    await userEvent.type(screen.getByTestId("login-password"), "pw");
    await userEvent.click(screen.getByRole("button", { name: "登录" }));
    release?.();

    await waitFor(() => expect(window.location.href).toBe("/"));
    expect(screen.getByRole("button", { name: "登录中…" })).toBeDisabled();
  });
});
