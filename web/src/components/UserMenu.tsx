import { ChevronDown, KeyRound, LogOut, UserRound } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Button } from "../ui";
import { ROLE_LABELS, type Role } from "../api/types";

/** 用户菜单：合并「用户名/角色信息」与「改密 / 退出」为单个下拉。 */
export function UserMenu({
  username,
  role,
  onChangePassword,
  onLogout,
}: {
  username: string;
  role: Role;
  onChangePassword: () => void;
  onLogout: () => void;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="sm" className="gap-1.5 px-2 sm:px-2.5" aria-label="用户菜单">
          <UserRound className="size-4" />
          <span className="hidden sm:inline">{username}</span>
          <ChevronDown className="hidden size-3.5 text-muted-foreground sm:inline" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-48">
        <DropdownMenuLabel className="text-xs font-normal text-muted-foreground">
          {username} · {ROLE_LABELS[role]}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          data-testid="menu-change-password"
          onSelect={onChangePassword}
          className="cursor-pointer"
        >
          <KeyRound className="mr-2 size-4" />
          修改密码
        </DropdownMenuItem>
        <DropdownMenuItem data-testid="menu-logout" onSelect={onLogout} className="cursor-pointer">
          <LogOut className="mr-2 size-4" />
          退出
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
