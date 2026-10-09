import { NavLink } from "react-router-dom";
import {
  BarChart3,
  BellRing,
  Boxes,
  Database,
  KeyRound,
  ScrollText,
  SlidersHorizontal,
  TerminalSquare,
  UsersRound,
} from "lucide-react";
import type { SessionInfo } from "../api/types";
import { cn } from "@/lib/utils";

export interface NavItem {
  to: string;
  label: string;
  icon: typeof Database;
  end?: boolean;
}

/** 桌面端一级导航，按使用频率排序。 */
export const NAV: NavItem[] = [
  { to: "/", label: "凭证管理", icon: Database, end: true },
  { to: "/stats", label: "用量统计", icon: BarChart3 },
  { to: "/models", label: "模型列表", icon: Boxes },
  { to: "/playground", label: "Playground", icon: TerminalSquare },
  { to: "/api-keys", label: "API Key", icon: KeyRound },
];

/** 管理类页面：仅 admin 可见。 */
export const ADMIN_NAV: NavItem[] = [
  { to: "/users", label: "用户管理", icon: UsersRound },
  { to: "/audit", label: "审计日志", icon: ScrollText },
  { to: "/alerts", label: "运维告警", icon: BellRing },
  { to: "/settings", label: "任务与配置", icon: SlidersHorizontal },
];

/** 品牌标记：多路渠道管道汇聚进单一出口（Coding2API 的产品故事）。 */
export function BrandMark({ className }: { className?: string }) {
  return (
    <span
      className={cn(
        "grid place-items-center rounded-lg bg-primary text-primary-foreground",
        className,
      )}
      aria-hidden
    >
      <svg
        viewBox="0 0 16 16"
        className="size-[60%]"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      >
        <path d="M2.5 3.5 6.5 8 2.5 12.5" />
        <path d="M13.5 3.5 9.5 8l4 4.5" />
        <path d="M6.5 8h4" />
        <circle cx="8" cy="8" r="1.4" fill="currentColor" stroke="none" />
      </svg>
    </span>
  );
}

function NavEntry({ item, onNavigate }: { item: NavItem; onNavigate?: () => void }) {
  return (
    <NavLink
      to={item.to}
      end={item.end}
      onClick={onNavigate}
      className={({ isActive }) =>
        cn(
          "group/nav flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition-colors",
          isActive
            ? "bg-primary/10 font-medium text-primary ring-1 ring-primary/20"
            : "text-muted-foreground hover:bg-muted hover:text-foreground",
        )
      }
    >
      <item.icon className="size-4 shrink-0" />
      <span className="truncate">{item.label}</span>
    </NavLink>
  );
}

/** 分组标题。 */
function GroupLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="px-2.5 pt-4 pb-1.5 text-[11px] font-semibold tracking-widest text-muted-foreground/80 uppercase">
      {children}
    </div>
  );
}

/**
 * 导航主体（分组导航）。
 *
 * 桌面端常驻侧栏；移动端同一份内容放进顶栏抽屉里复用（onNavigate 在跳转后关抽屉）。
 * 管理组仅 admin 可见——写接口是 admin-only，露出入口只会误导。
 * 仓库入口在顶栏（Layout），不在这里。
 */
export function SidebarNav({
  session,
  onNavigate,
}: {
  session: SessionInfo;
  onNavigate?: () => void;
}) {
  const adminNav = session.is_admin ? ADMIN_NAV : [];
  return (
    <nav className="flex flex-1 flex-col gap-0.5 overflow-y-auto px-2 py-2" aria-label="主导航">
      <GroupLabel>控制台</GroupLabel>
      {NAV.map((item) => (
        <NavEntry key={item.to} item={item} onNavigate={onNavigate} />
      ))}
      {adminNav.length > 0 && (
        <>
          <GroupLabel>管理</GroupLabel>
          {adminNav.map((item) => (
            <NavEntry key={item.to} item={item} onNavigate={onNavigate} />
          ))}
        </>
      )}
    </nav>
  );
}
