import { NavLink } from "react-router-dom";
import {
  BarChart3,
  BellRing,
  Boxes,
  Database,
  DatabaseBackup,
  FileTerminal,
  KeyRound,
  RadioTower,
  Receipt,
  ScrollText,
  SlidersHorizontal,
  TerminalSquare,
  UsersRound,
} from "lucide-react";
import type { NavBadgeKey, NavBadges, SessionInfo } from "../api/types";
import { navBadgeView } from "@/lib/navBadges";
import { NavBadge } from "@/ui";
import { cn } from "@/lib/utils";

export interface NavItem {
  to: string;
  label: string;
  icon: typeof Database;
  end?: boolean;
  /** 操作员（含 admin）也可见；缺省 = 仅 admin。与路由守卫同一门槛。 */
  operator?: boolean;
  /** 角标数据源键（见 `GET /api/nav-badges`）；缺省 = 该项不挂角标。 */
  badgeKey?: NavBadgeKey;
}

/** 桌面端一级导航，按使用频率排序。 */
export const NAV: NavItem[] = [
  { to: "/", label: "凭证管理", icon: Database, end: true, badgeKey: "credentials" },
  { to: "/stats", label: "用量统计", icon: BarChart3 },
  { to: "/playground", label: "Playground", icon: TerminalSquare },
  { to: "/api-keys", label: "API Key", icon: KeyRound, badgeKey: "api_keys" },
];

/**
 * 管理类页面。默认仅 admin 可见；标了 `operator` 的项对操作员也开放。
 *
 * 门槛与后端一致：admin-only 的项（用户管理 / 审计 / 告警 / 任务与配置）
 * 对应 `require_admin`，`operator` 项（上游状态 / 模型列表）对应
 * `require_operator`。露出入口而不给权限只会误导。
 *
 * `badgeKey` 同样按角色裁剪（见 `GET /api/nav-badges`）：后端不下发的键前端
 * 自然不渲染角标，无需在这里重复判角色。
 */
export const ADMIN_NAV: NavItem[] = [
  { to: "/users", label: "用户管理", icon: UsersRound, badgeKey: "users" },
  { to: "/audit", label: "审计日志", icon: ScrollText },
  { to: "/logs", label: "服务端日志", icon: FileTerminal },
  { to: "/alerts", label: "运维告警", icon: BellRing, badgeKey: "alerts" },
  { to: "/backups", label: "备份与恢复", icon: DatabaseBackup },
  { to: "/upstream", label: "上游状态", icon: RadioTower, operator: true, badgeKey: "upstream" },
  { to: "/billing", label: "月度账单", icon: Receipt, operator: true },
  { to: "/models", label: "模型列表", icon: Boxes, operator: true,
    badgeKey: "model_catalog_stale" },
  { to: "/settings", label: "任务与配置", icon: SlidersHorizontal, badgeKey: "tasks" },
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

function NavEntry({
  item,
  badges,
  onNavigate,
}: {
  item: NavItem;
  badges?: NavBadges;
  onNavigate?: () => void;
}) {
  const badge = item.badgeKey ? navBadgeView(item.badgeKey, badges) : null;
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
      {badge && <NavBadge value={badge.value} tone={badge.tone} label={badge.label} />}
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
 * 管理组对 admin 全量可见、对 operator 只显示 `operator` 项（与路由守卫同一口径），
 * viewer 整组不可见。
 * 仓库入口在顶栏（Layout），不在这里。
 *
 * `badges` 由 Layout 的 `useNavBadges` 下发（未加载 / 失败时为 undefined → 全部
 * 不渲染角标）；角色裁剪已在后端完成，这里不做二次判断。
 */
export function SidebarNav({
  session,
  badges,
  onNavigate,
}: {
  session: SessionInfo;
  badges?: NavBadges;
  onNavigate?: () => void;
}) {
  const adminNav = ADMIN_NAV.filter(
    (item) => session.is_admin || (session.is_operator && item.operator),
  );
  return (
    <nav className="flex flex-1 flex-col gap-0.5 overflow-y-auto px-2 py-2" aria-label="主导航">
      <GroupLabel>控制台</GroupLabel>
      {NAV.map((item) => (
        <NavEntry key={item.to} item={item} badges={badges} onNavigate={onNavigate} />
      ))}
      {adminNav.length > 0 && (
        <>
          <GroupLabel>管理</GroupLabel>
          {adminNav.map((item) => (
            <NavEntry key={item.to} item={item} badges={badges} onNavigate={onNavigate} />
          ))}
        </>
      )}
    </nav>
  );
}
