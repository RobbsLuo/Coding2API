import { Component, type ErrorInfo, type ReactNode } from "react";
import { RotateCcw, TriangleAlert } from "lucide-react";
import { Button } from "./ui/button";

type Props = { children: ReactNode };
type State = { error: Error | null };

/**
 * 渲染错误兜底：任何页面组件抛错时给一页可读的失败面板，而不是白屏。
 *
 * 两处挂载：
 * - `main.tsx` 顶层：兜住登录页 / 路由本身 / 懒加载 chunk 失败的异常；
 * - `Layout` 的 `<Outlet/>` 外层（`key=pathname`）：单页崩溃时保留侧栏与顶栏，
 *   用户仍能切到别的页面，且路由变化会重建边界、自动清除错误态。
 *
 * 只做「失败可见 + 可恢复」，不吞错误：`componentDidCatch` 照常写 console，
 * 便于从 DevTools 定位组件栈。
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("页面渲染失败", error, info.componentStack);
  }

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div
        role="alert"
        data-testid="error-boundary"
        className="grid min-h-[50vh] place-items-center p-6"
      >
        <div className="w-full max-w-md space-y-3 rounded-xl border border-border bg-card p-5 shadow-sm">
          <div className="flex items-center gap-2 text-sm font-semibold">
            <TriangleAlert className="size-4 text-[var(--color-danger,#dc2626)]" />
            页面出错了
          </div>
          <p className="text-sm text-muted-foreground">
            这个页面渲染时遇到异常，其他功能不受影响。可以重载页面重试；若反复出现，
            请把下面的错误信息与浏览器控制台日志一并反馈。
          </p>
          <pre
            data-testid="error-boundary-message"
            className="max-h-40 overflow-auto rounded-md bg-muted p-2 text-xs whitespace-pre-wrap break-words"
          >
            {error.message || String(error)}
          </pre>
          <div className="flex justify-end gap-2">
            <Button asChild variant="ghost">
              <a href="/">回到首页</a>
            </Button>
            <Button
              variant="default"
              data-testid="error-boundary-reload"
              onClick={() => window.location.reload()}
            >
              <RotateCcw className="size-3.5" /> 重新加载
            </Button>
          </div>
        </div>
      </div>
    );
  }
}
