import { useEffect, useRef } from "react";

/**
 * 对话框焦点管理（无障碍）。
 *
 * 把返回的 ref 挂到已存在的 `role="dialog"` 元素上，即可获得：
 * - Esc 关闭（`onDismiss` 为空时禁用，如强制改密弹窗）
 * - 打开时把焦点移入（第一个可聚焦元素，没有则聚焦容器本身）
 * - Tab / Shift+Tab 焦点循环（不让焦点跑出对话框）
 * - 关闭时把焦点还给打开它的元素
 * - 打开期间锁定 body 滚动
 *
 * `onDismiss` 存进 ref，避免父组件每次渲染换函数引用导致重复聚焦/重跑 effect。
 * `active` 用于「常驻挂载、按需显示」的对话框（如移动端抽屉）；默认 true。
 */
const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

export function useDialogFocus<T extends HTMLElement>(
  onDismiss?: () => void,
  active = true,
) {
  const ref = useRef<T>(null);
  const dismissRef = useRef(onDismiss);
  dismissRef.current = onDismiss;

  useEffect(() => {
    if (!active) return;
    const node = ref.current;
    if (!node) return;

    const previouslyFocused = document.activeElement as HTMLElement | null;
    const focusables = () =>
      Array.from(node.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (el) => !el.hasAttribute("hidden") && el.getAttribute("aria-hidden") !== "true",
      );

    // 初始聚焦：优先第一个可聚焦元素，否则让容器自己接住焦点。
    const first = focusables()[0];
    if (first) {
      first.focus();
    } else {
      node.tabIndex = -1;
      node.focus();
    }

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        if (dismissRef.current) {
          event.preventDefault();
          dismissRef.current();
        }
        return;
      }
      if (event.key !== "Tab") return;
      // 焦点陷阱：在首尾之间循环。
      const items = focusables();
      if (items.length === 0) {
        event.preventDefault();
        node.focus();
        return;
      }
      const firstItem = items[0];
      const lastItem = items[items.length - 1];
      const activeEl = document.activeElement;
      if (event.shiftKey && (activeEl === firstItem || activeEl === node)) {
        event.preventDefault();
        lastItem.focus();
      } else if (!event.shiftKey && activeEl === lastItem) {
        event.preventDefault();
        firstItem.focus();
      }
    };

    node.addEventListener("keydown", onKeyDown);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    return () => {
      node.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      previouslyFocused?.focus?.();
    };
  }, [active]);

  return ref;
}