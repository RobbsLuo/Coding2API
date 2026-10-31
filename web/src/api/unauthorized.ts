/**
 * 401 的统一出口：会话过期（Cookie 失效 / 被撤销）时前端不能停在错误态。
 *
 * `client.ts` 不认识 react-query，也不该反向依赖 `main.tsx`；这里只放一个
 * 可注册的处理器，由入口在启动时把「清缓存 + 回登录页」接上。保持单向依赖：
 * client → unauthorized（无依赖），main → 两者。
 */

type UnauthorizedHandler = () => void;

let handler: UnauthorizedHandler | null = null;

/** 注册会话过期处理器（`main.tsx` 启动时调用；传 null 可注销）。 */
export function setUnauthorizedHandler(next: UnauthorizedHandler | null): void {
  handler = next;
}

/** 通知会话过期；未注册处理器时静默（单测与 SSR 场景）。 */
export function notifyUnauthorized(): void {
  handler?.();
}

/**
 * 免登录端点：这些路径上的 401 是业务语义而不是会话过期。
 *
 * - `/api/auth/session`：未登录时的正常应答（App 据此渲染登录页）；
 * - `/api/auth/login`：密码错误；
 * - `/api/auth/activate`：激活令牌无效/过期。
 *
 * 若把它们算作会话过期，密码输错就会把用户「踢」一次并清空缓存。
 */
const ANONYMOUS_PATHS = ["/api/auth/session", "/api/auth/login", "/api/auth/activate"];

/** 该响应是否代表「会话过期」（需要清缓存并回登录页）。 */
export function isSessionExpiry(path: string, status: number): boolean {
  return status === 401 && !ANONYMOUS_PATHS.some((prefix) => path.startsWith(prefix));
}