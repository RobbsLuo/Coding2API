import { describe, expect, it, vi } from "vitest";
import {
  isSessionExpiry,
  notifyUnauthorized,
  setUnauthorizedHandler,
} from "../unauthorized";

describe("unauthorized（401 统一出口）", () => {
  it("受保护端点的 401 视为会话过期", () => {
    expect(isSessionExpiry("/api/users", 401)).toBe(true);
    expect(isSessionExpiry("/api/api-keys", 401)).toBe(true);
  });

  it("免登录端点的 401 是业务语义，不算会话过期", () => {
    for (const path of ["/api/auth/session", "/api/auth/login", "/api/auth/activate"]) {
      expect(isSessionExpiry(path, 401)).toBe(false);
    }
  });

  it("非 401 状态不算会话过期", () => {
    expect(isSessionExpiry("/api/users", 403)).toBe(false);
    expect(isSessionExpiry("/api/users", 500)).toBe(false);
    expect(isSessionExpiry("/api/users", 200)).toBe(false);
  });

  it("注册后通知处理器；注销后不再调用", () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    notifyUnauthorized();
    expect(handler).toHaveBeenCalledTimes(1);

    setUnauthorizedHandler(null);
    notifyUnauthorized();
    expect(handler).toHaveBeenCalledTimes(1);
  });
});