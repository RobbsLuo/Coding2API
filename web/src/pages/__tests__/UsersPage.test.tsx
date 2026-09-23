import { screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { UsersPage } from "../UsersPage";
import { jsonResponse, renderPage, settle, userEvent } from "./helpers";

/** 按「METHOD path」分派；条目顺序即匹配优先级（长的写前面）。 */
function stubFetch(routes: Record<string, () => Response>) {
  const spy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    const method = init?.method ?? "GET";
    for (const [key, handler] of Object.entries(routes)) {
      const space = key.indexOf(" ");
      if (method === key.slice(0, space) && url.includes(key.slice(space + 1))) {
        return handler();
      }
    }
    throw new Error(`未 mock 的请求: ${method} ${url}`);
  });
  vi.stubGlobal("fetch", spy);
  return spy;
}

const USERS = {
  users: [
    {
      username: "root", role: "admin", enabled: true, must_change_password: false,
      created_at: 1_700_000_000, updated_at: 1_700_000_000, created_by: null,
      pending_activation: false,
    },
    {
      username: "alice", role: "operator", enabled: true, must_change_password: true,
      created_at: 1_700_000_100, updated_at: 1_700_000_100, created_by: "root",
      pending_activation: true,
    },
    {
      username: "bob", role: "viewer", enabled: false, must_change_password: false,
      created_at: 1_700_000_200, updated_at: 1_700_000_200, created_by: "root",
      pending_activation: false,
    },
  ],
};

describe("UsersPage", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("列出用户、角色与状态徽章", async () => {
    stubFetch({ "GET /api/users": () => jsonResponse(USERS) });
    renderPage(<UsersPage />, { username: "root", is_admin: true });
    await settle();

    expect(screen.getByTestId("users-table")).toBeInTheDocument();
    expect(screen.getByTestId("user-row-root")).toHaveTextContent("（我）");
    expect(screen.getByTestId("role-alice")).toHaveValue("operator");
    // bob 被禁用 + alice 待激活/需改密
    expect(screen.getByTestId("user-row-bob")).toHaveTextContent("已禁用");
    expect(screen.getByTestId("user-row-alice")).toHaveTextContent("待激活");
    expect(screen.getByTestId("user-row-alice")).toHaveTextContent("需改密");
    // 引导导入的用户没有 created_by
    expect(screen.getByTestId("user-row-root")).toHaveTextContent("引导导入");
  });

  it("空列表显示占位", async () => {
    stubFetch({ "GET /api/users": () => jsonResponse({ users: [] }) });
    renderPage(<UsersPage />, { username: "root", is_admin: true });
    await settle();
    expect(screen.getByTestId("no-users")).toBeInTheDocument();
  });

  it("创建用户后一次性展示激活链接", async () => {
    const copied: string[] = [];
    vi.stubGlobal("navigator", {
      clipboard: { writeText: async (text: string) => void copied.push(text) },
    });
    stubFetch({
      "POST /api/users": () => jsonResponse({
        username: "newbie", role: "viewer", activate_token: "TOK+EN", expires_at: 1_800_000_000,
      }),
      "GET /api/users": () => jsonResponse(USERS),
    });
    renderPage(<UsersPage />, { username: "root", is_admin: true });
    await settle();

    await userEvent.type(screen.getByTestId("new-user-name"), "newbie");
    await userEvent.click(screen.getByRole("button", { name: /创建/ }));

    const link = await screen.findByTestId("activation-link");
    // token 出现在链接里（URL 编码）
    expect(link).toHaveTextContent("/activate?token=TOK%2BEN");
    await userEvent.click(screen.getByTestId("copy-activation"));
    expect(copied[0]).toContain("/activate?token=TOK%2BEN");
    expect(await screen.findByText("已复制")).toBeInTheDocument();
    // 「我已转交」后卡片消失（明文不再可见）
    await userEvent.click(screen.getByRole("button", { name: "我已转交" }));
    await waitFor(() => expect(screen.queryByTestId("activation-link")).not.toBeInTheDocument());
  });

  it("创建失败（重名）时给出提示", async () => {
    stubFetch({
      "POST /api/users": () =>
        jsonResponse({ error: { code: "invalid_request", message: "user 'alice' already exists" } }, 400),
      "GET /api/users": () => jsonResponse(USERS),
    });
    renderPage(<UsersPage />, { username: "root", is_admin: true });
    await settle();
    await userEvent.type(screen.getByTestId("new-user-name"), "alice");
    await userEvent.click(screen.getByRole("button", { name: /创建/ }));
    expect(await screen.findByTestId("users-error")).toHaveTextContent("创建失败");
  });

  it("改角色调用 PATCH", async () => {
    const spy = stubFetch({
      "PATCH /api/users/alice": () => jsonResponse({ ok: true }),
      "GET /api/users": () => jsonResponse(USERS),
    });
    renderPage(<UsersPage />, { username: "root", is_admin: true });
    await settle();

    await userEvent.selectOptions(screen.getByTestId("role-alice"), "admin");
    await waitFor(() => {
      const call = spy.mock.calls.find(([, init]) => init?.method === "PATCH");
      expect(call?.[0]).toBe("/api/users/alice");
      expect(JSON.parse(String(call?.[1]?.body))).toEqual({ role: "admin" });
    });
  });

  it("降级最后一个 admin 时把后端错误翻成人话", async () => {
    stubFetch({
      "PATCH /api/users/alice": () =>
        jsonResponse({ error: { code: "last_admin", message: "cannot remove the last active admin" } }, 400),
      "GET /api/users": () => jsonResponse(USERS),
    });
    renderPage(<UsersPage />, { username: "root", is_admin: true });
    await settle();
    await userEvent.selectOptions(screen.getByTestId("role-alice"), "viewer");
    expect(await screen.findByTestId("users-error")).toHaveTextContent("最后一个活跃管理员");
  });

  it("对自己动手时提示让别的管理员处理", async () => {
    stubFetch({
      "PATCH /api/users/root": () =>
        jsonResponse({ error: { code: "self_target", message: "cannot change your own role" } }, 400),
      "GET /api/users": () => jsonResponse(USERS),
    });
    renderPage(<UsersPage />, { username: "root", is_admin: true });
    await settle();
    await userEvent.selectOptions(screen.getByTestId("role-root"), "viewer");
    expect(await screen.findByTestId("users-error")).toHaveTextContent("不能对自己执行此操作");
  });

  it("禁用与启用走各自端点", async () => {
    const spy = stubFetch({
      "POST /api/users/alice/disable": () => jsonResponse({ ok: true }),
      "POST /api/users/bob/enable": () => jsonResponse({ ok: true }),
      "GET /api/users": () => jsonResponse(USERS),
    });
    renderPage(<UsersPage />, { username: "root", is_admin: true });
    await settle();

    await userEvent.click(screen.getByTestId("toggle-alice"));
    await userEvent.click(screen.getByTestId("toggle-bob"));
    await waitFor(() => {
      const urls = spy.mock.calls
        .filter(([, init]) => init?.method === "POST")
        .map(([url]) => url);
      expect(urls).toContain("/api/users/alice/disable");
      expect(urls).toContain("/api/users/bob/enable");
    });
  });

  it("禁用失败（最后 admin）也回显提示", async () => {
    stubFetch({
      "POST /api/users/root/disable": () =>
        jsonResponse({ error: { code: "last_admin", message: "cannot remove the last active admin" } }, 400),
      "GET /api/users": () => jsonResponse(USERS),
    });
    renderPage(<UsersPage />, { username: "root", is_admin: true });
    await settle();
    await userEvent.click(screen.getByTestId("toggle-root"));
    expect(await screen.findByTestId("users-error")).toHaveTextContent("最后一个活跃管理员");
  });

  it("重置密码展示新的激活链接", async () => {
    stubFetch({
      "POST /api/users/alice/reset-password": () => jsonResponse({
        username: "alice", activate_token: "RESET", expires_at: 1_800_000_000,
      }),
      "GET /api/users": () => jsonResponse(USERS),
    });
    renderPage(<UsersPage />, { username: "root", is_admin: true });
    await settle();
    await userEvent.click(screen.getByTestId("reset-alice"));
    expect(await screen.findByTestId("activation-link")).toHaveTextContent("token=RESET");
  });

  it("重置密码失败提示", async () => {
    stubFetch({
      "POST /api/users/alice/reset-password": () =>
        jsonResponse({ error: { code: "invalid_request", message: "boom" } }, 400),
      "GET /api/users": () => jsonResponse(USERS),
    });
    renderPage(<UsersPage />, { username: "root", is_admin: true });
    await settle();
    await userEvent.click(screen.getByTestId("reset-alice"));
    expect(await screen.findByTestId("users-error")).toHaveTextContent("重置密码失败");
  });
});
