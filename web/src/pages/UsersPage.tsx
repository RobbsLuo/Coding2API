import { useState } from "react";
import { Check, Copy, KeyRound, Plus, RefreshCw, UserRoundCheck, UserRoundX } from "lucide-react";
import { api, ApiError } from "../api/client";
import { formatTime } from "../api/display";
import { useSessionContext } from "../Layout";
import { useUserMutation, useUsers } from "../api/hooks";
import type { ActivationIssued, Role, UserRow } from "../api/types";
import { ROLE_LABELS, ROLE_OPTIONS } from "../api/types";
import { PageHeader } from "../components/PageHeader";
import {
  Badge,
  Button,
  Empty,
  Field,
  Input,
  Notice,
  Panel,
  Select,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../ui";

/** 一次性激活令牌展示卡：明文只出现这一次，与 API Key 同一心智模型。 */
function TokenCard({
  issued,
  onDismiss,
}: {
  issued: ActivationIssued;
  onDismiss: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const link = `${window.location.origin}/activate?token=${encodeURIComponent(issued.activate_token)}`;
  const copy = async () => {
    await navigator.clipboard.writeText(link);
    setCopied(true);
  };
  return (
    <Panel title={`${issued.username} 的激活链接`}>
      <Notice tone="warn">
        此链接只显示这一次，请立即复制并通过可信渠道转交本人；有效期至{" "}
        {formatTime(issued.expires_at)}。
      </Notice>
      <div className="mt-3 flex items-center gap-2">
        <code
          data-testid="activation-link"
          className="flex-1 truncate rounded-lg border border-input bg-muted/40 px-3 py-2 font-mono text-xs"
        >
          {link}
        </code>
        <Button size="sm" variant="ghost" onClick={copy} data-testid="copy-activation">
          {copied ? <Check className="mr-1 size-3.5" /> : <Copy className="mr-1 size-3.5" />}
          {copied ? "已复制" : "复制"}
        </Button>
        <Button size="sm" variant="ghost" onClick={onDismiss}>
          我已转交
        </Button>
      </div>
    </Panel>
  );
}

export function UsersPage() {
  const session = useSessionContext();
  const { data, isLoading } = useUsers(session.username);
  const [username, setUsername] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const [issued, setIssued] = useState<ActivationIssued | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const create = useUserMutation(
    (args: { username: string; role: Role }) => api.createUser(args.username, args.role),
    session.username,
  );
  const updateRole = useUserMutation(
    (args: { username: string; role: Role }) => api.updateUserRole(args.username, args.role),
    session.username,
  );
  const setEnabled = useUserMutation(
    (args: { username: string; enabled: boolean }) =>
      args.enabled ? api.enableUser(args.username) : api.disableUser(args.username),
    session.username,
  );
  const resetPassword = useUserMutation(
    (args: { username: string }) => api.resetUserPassword(args.username),
    session.username,
  );

  const users = data?.users ?? [];

  const report = (caught: unknown, fallback: string) => {
    const message = caught instanceof ApiError ? caught.message : "";
    if (message.includes("last active admin")) {
      setError("不能移除最后一个活跃管理员，否则将无人能管理此系统。");
    } else if (/your own|yourself/.test(message)) {
      setError("不能对自己执行此操作，请让另一位管理员处理。");
    } else {
      setError(fallback);
    }
  };

  const submitCreate = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    setIssued(null);
    try {
      const result = await create.mutateAsync({ username: username.trim(), role });
      setIssued(result);
      setUsername("");
      setRole("viewer");
    } catch (caught) {
      report(caught, "创建失败：用户名可能已存在，或角色非法。");
    }
  };

  const changeRole = async (user: UserRow, next: Role) => {
    setError(null);
    setBusy(user.username);
    try {
      await updateRole.mutateAsync({ username: user.username, role: next });
    } catch (caught) {
      report(caught, "变更角色失败。");
    } finally {
      setBusy(null);
    }
  };

  const toggleEnabled = async (user: UserRow) => {
    setError(null);
    setBusy(user.username);
    try {
      await setEnabled.mutateAsync({ username: user.username, enabled: !user.enabled });
    } catch (caught) {
      report(caught, "变更启用状态失败。");
    } finally {
      setBusy(null);
    }
  };

  const issueReset = async (user: UserRow) => {
    setError(null);
    setIssued(null);
    setBusy(user.username);
    try {
      setIssued(await resetPassword.mutateAsync({ username: user.username }));
    } catch (caught) {
      report(caught, "重置密码失败。");
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="space-y-6" data-testid="users-page">
      <PageHeader
        title="用户管理"
        description="创建账号、分配角色、停用或重置密码。新建账号会得到一次性激活链接，由本人自设密码——系统里不存在管理员已知的共享密码。"
        icon={<KeyRound className="size-5" />}
      />

      <Panel title="新建用户">
        <form onSubmit={submitCreate} className="flex flex-wrap items-end gap-3">
          <div className="w-56">
            <Field label="用户名">
              <Input
                value={username}
                data-testid="new-user-name"
                placeholder="例如 alice"
                onChange={(event) => setUsername(event.target.value)}
              />
            </Field>
          </div>
          <div className="w-44">
            <Field label="角色">
              <Select
                value={role}
                data-testid="new-user-role"
                onChange={(event) => setRole(event.target.value as Role)}
              >
                {ROLE_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
          <Button type="submit" variant="primary" disabled={create.isPending || !username.trim()}>
            <Plus className="mr-1 size-4" />
            创建
          </Button>
        </form>
        {error && (
          <div className="mt-3" data-testid="users-error">
            <Notice tone="danger">{error}</Notice>
          </div>
        )}
      </Panel>

      {issued && <TokenCard issued={issued} onDismiss={() => setIssued(null)} />}

      <Panel title="全部用户">
        {isLoading ? (
          <Empty>载入中…</Empty>
        ) : users.length === 0 ? (
          <Empty data-testid="no-users">还没有用户</Empty>
        ) : (
          <Table data-testid="users-table">
            <TableHeader>
              <TableRow>
                <TableHead>用户名</TableHead>
                <TableHead>角色</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>创建者</TableHead>
                <TableHead>创建时间</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {users.map((user) => (
                <TableRow key={user.username} data-testid={`user-row-${user.username}`}>
                  <TableCell className="font-medium">
                    {user.username}
                    {user.username === session.username && (
                      <span className="ml-1.5 text-xs text-muted-foreground">（我）</span>
                    )}
                  </TableCell>
                  <TableCell>
                    <Select
                      value={user.role}
                      disabled={busy === user.username}
                      aria-label={`${user.username} 的角色`}
                      data-testid={`role-${user.username}`}
                      onChange={(event) => void changeRole(user, event.target.value as Role)}
                    >
                      {ROLE_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </Select>
                  </TableCell>
                  <TableCell>
                    {user.enabled ? (
                      <Badge tone="ok">启用</Badge>
                    ) : (
                      <Badge tone="danger">已禁用</Badge>
                    )}
                    {user.pending_activation && (
                      <span className="ml-1.5">
                        <Badge tone="warn">待激活</Badge>
                      </span>
                    )}
                    {user.must_change_password && (
                      <span className="ml-1.5">
                        <Badge tone="warn">需改密</Badge>
                      </span>
                    )}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {user.created_by ?? "引导导入"}
                  </TableCell>
                  <TableCell className="text-xs">{formatTime(user.created_at)}</TableCell>
                  <TableCell className="text-right">
                    <span className="inline-flex gap-1.5">
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={busy === user.username}
                        data-testid={`reset-${user.username}`}
                        onClick={() => void issueReset(user)}
                      >
                        <RefreshCw className="mr-1 size-3.5" />
                        重置密码
                      </Button>
                      <Button
                        size="sm"
                        variant={user.enabled ? "danger" : "default"}
                        disabled={busy === user.username}
                        data-testid={`toggle-${user.username}`}
                        onClick={() => void toggleEnabled(user)}
                      >
                        {user.enabled ? (
                          <>
                            <UserRoundX className="mr-1 size-3.5" />
                            禁用
                          </>
                        ) : (
                          <>
                            <UserRoundCheck className="mr-1 size-3.5" />
                            启用
                          </>
                        )}
                      </Button>
                    </span>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Panel>

      <Notice tone="muted">
        角色：{ROLE_LABELS.admin} 可管理用户与配置；{ROLE_LABELS.operator}{" "}
        可增删凭证；{ROLE_LABELS.viewer} 只读。禁用、改角色与重置密码都会立即使其所有会话失效。
      </Notice>
    </div>
  );
}
