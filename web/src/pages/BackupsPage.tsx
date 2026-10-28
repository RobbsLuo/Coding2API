import { useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { DatabaseBackup, Download, RotateCcw, Trash2, Upload } from "lucide-react";
import { api, ApiError } from "../api/client";
import { adminKey } from "../api/hooks";
import { formatTime } from "../api/display";
import type { BackupEntry } from "../api/types";
import { Button as RawButton } from "../components/ui/button";
import { useSessionContext } from "../Layout";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { useDialogFocus } from "../hooks/useDialogFocus";
import {
  Button,
  EmptyState,
  Notice,
  Panel,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../ui";

/** 字节数 → 人类可读；备份是整库文件，MB 以下用 KB 更直观。 */
export function formatBytes(size: number): string {
  if (size >= 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
  if (size >= 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${size} B`;
}

type Pending =
  | { kind: "delete"; name: string }
  | { kind: "restore"; name: string }
  | { kind: "upload"; file: File };

/**
 * 备份与恢复。
 *
 * 恢复不在运行中换库：上传 / 选中的备份先由后端校验并暂存，**重启服务后**
 * 才在建连接之前原子替换。页面必须把这个时序说清楚，否则用户会以为点完
 * 就生效、立刻去验证数据。
 */
export function BackupsPage() {
  const session = useSessionContext();
  const queryClient = useQueryClient();
  const inputRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);

  const { data, isLoading, isError } = useQuery({
    queryKey: adminKey(session.username, "backups"),
    queryFn: api.backups,
  });
  const backups = data?.backups ?? [];
  const stagedRestore = data?.pending ?? null;

  const refresh = () => queryClient.invalidateQueries({
    queryKey: adminKey(session.username, "backups"),
  });
  const fail = (caught: unknown) => {
    setError(caught instanceof ApiError ? caught.message : "操作失败，请稍后重试");
  };

  const createBackup = async () => {
    setBusy(true);
    setError(null);
    try {
      const created = await api.createBackup();
      setNotice(`已创建备份 ${created.backup.name}`);
      await refresh();
    } catch (caught) {
      fail(caught);
    } finally {
      setBusy(false);
    }
  };

  const runPending = async () => {
    if (!pending) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      if (pending.kind === "delete") {
        await api.deleteBackup(pending.name);
        setNotice(`已删除 ${pending.name}`);
      } else if (pending.kind === "restore") {
        const blob = await api.downloadBackup(pending.name);
        await api.restoreBackup(blob, pending.name);
        setNotice(`已将「${pending.name}」暂存为待恢复备份；重启服务后生效`);
      } else {
        await api.restoreBackup(pending.file, pending.file.name);
        setNotice(`已上传「${pending.file.name}」；重启服务后生效`);
      }
      await refresh();
      setPending(null);
    } catch (caught) {
      fail(caught);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-6" data-testid="backups-page">
      <PageHeader
        eyebrow="运维"
        title="备份与恢复"
        description="备份是整库一致性快照（VACUUM INTO + 完整性校验），只留在服务器 DATA_DIR/backups，下载后请自行保管。恢复采用「上传校验 → 暂存 → 下次启动生效」：运行中不换库，避免旧连接与 WAL 把新库拼坏；启动时若待恢复文件校验不过，会留档并继续用当前库。"
        icon={<DatabaseBackup className="size-5" />}
        actions={
          <>
            <input
              ref={inputRef}
              type="file"
              accept=".sqlite3,.db,application/x-sqlite3,application/octet-stream"
              className="hidden"
              data-testid="backup-file"
              onChange={(event) => {
                const file = event.target.files?.[0];
                event.target.value = "";
                if (file) setPending({ kind: "upload", file });
              }}
            />
            <Button
              size="sm"
              variant="ghost"
              data-testid="open-restore-upload"
              disabled={busy}
              onClick={() => inputRef.current?.click()}
            >
              <Upload className="size-4" /> 上传并恢复
            </Button>
            <Button
              size="sm"
              variant="default"
              data-testid="create-backup"
              disabled={busy}
              onClick={() => void createBackup()}
            >
              <DatabaseBackup className="size-4" /> 立即备份
            </Button>
          </>
        }
      />

      {stagedRestore && (
        <div data-testid="pending-restore">
          <Notice tone="warn">
            待恢复：「{stagedRestore.filename}」（{formatBytes(stagedRestore.size)}，上传于{" "}
            {formatTime(stagedRestore.uploaded_at)}）。重启服务后生效，启动时会先为当前库
            留一份 `.pre-restore-*` 安全副本。
          </Notice>
        </div>
      )}

      {notice && (
        <div data-testid="backup-notice">
          <Notice tone="ok">{notice}</Notice>
        </div>
      )}
      {error && (
        <div data-testid="backup-error" role="alert">
          <Notice tone="danger">{error}</Notice>
        </div>
      )}

      <Panel title="备份文件">
        {isLoading ? (
          <PageSkeleton variant="table" rows={3} />
        ) : isError ? (
          <div data-testid="backups-load-error">
            <Notice tone="danger">备份列表加载失败，请刷新重试。</Notice>
          </div>
        ) : backups.length === 0 ? (
          <EmptyState
            icon={<DatabaseBackup className="size-5" />}
            title="还没有备份"
            description="后台任务会按配置周期自动备份；也可以点右上角「立即备份」。"
          />
        ) : (
          <Table data-testid="backups-table">
            <TableHeader>
              <TableRow>
                <TableHead>文件名</TableHead>
                <TableHead>大小</TableHead>
                <TableHead>时间</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {backups.map((item: BackupEntry) => (
                <TableRow key={item.name} data-testid={`backup-row-${item.name}`}>
                  <TableCell className="font-mono text-xs">{item.name}</TableCell>
                  <TableCell>{formatBytes(item.size)}</TableCell>
                  <TableCell>{formatTime(item.created_at)}</TableCell>
                  <TableCell className="text-right">
                    <div className="flex justify-end gap-1">
                      <RawButton size="sm" variant="ghost" asChild>
                        <a
                          href={`/api/backups/${encodeURIComponent(item.name)}`}
                          download
                          data-testid={`download-${item.name}`}
                        >
                          <Download className="size-3.5" /> 下载
                        </a>
                      </RawButton>
                      <Button
                        size="sm"
                        variant="ghost"
                        data-testid={`restore-${item.name}`}
                        disabled={busy}
                        onClick={() => setPending({ kind: "restore", name: item.name })}
                      >
                        <RotateCcw className="size-3.5" /> 恢复
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        data-testid={`delete-${item.name}`}
                        disabled={busy}
                        onClick={() => setPending({ kind: "delete", name: item.name })}
                      >
                        <Trash2 className="size-3.5" /> 删除
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Panel>

      {pending && (
        <ConfirmDialog
          pending={pending}
          busy={busy}
          onCancel={() => setPending(null)}
          onConfirm={() => void runPending()}
        />
      )}
    </div>
  );
}

function ConfirmDialog({ pending, busy, onCancel, onConfirm }: {
  pending: Pending;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const dialogRef = useDialogFocus<HTMLDivElement>(busy ? undefined : onCancel);
  const restore = pending.kind !== "delete";
  const title = pending.kind === "delete" ? "删除备份"
    : pending.kind === "restore" ? "恢复到这个备份"
    : "上传并恢复";
  const detail = pending.kind === "delete"
    ? `将从服务器删除 ${pending.name}，不可撤销。`
    : pending.kind === "restore"
      ? `将把「${pending.name}」暂存为待恢复备份；重启服务后覆盖当前数据库（启动前自动留安全副本）。`
      : `将上传「${pending.file.name}」并暂存；重启服务后覆盖当前数据库（启动前自动留安全副本）。`;
  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/50 p-4 backdrop-blur-sm">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        data-testid="backup-confirm"
        className="w-full max-w-md space-y-3 rounded-xl border border-border bg-card p-5 shadow-xl"
      >
        <h2 className="text-sm font-semibold">{title}</h2>
        <p className="text-sm text-muted-foreground">{detail}</p>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" data-testid="backup-confirm-cancel"
                  onClick={onCancel} disabled={busy}>
            取消
          </Button>
          <Button
            variant={restore ? "primary" : "danger"}
            data-testid="backup-confirm-ok"
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? "处理中…" : restore ? "暂存恢复" : "删除"}
          </Button>
        </div>
      </div>
    </div>
  );
}
