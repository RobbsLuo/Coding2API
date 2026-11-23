import { Gauge, Pencil, Share2, Trash2 } from "lucide-react";
import { type ApiKeyWindow, USAGE_WINDOW_LABELS } from "../../api/apiKeys";
import { formatTime } from "../../api/display";
import {
  quotaOf,
  quotaSummary,
  usageLabel,
  usageOf,
} from "../../api/keyQuota";
import type { ApiKey } from "../../api/types";
import { Button, TableCell, TableRow } from "../../ui";
import { providerBindingLabel } from "./meta";

/**
 * 列表行：纯展示 + 行内动作回调。
 *
 * 行内状态（删除二次确认、正在编辑的配额/策略、导入对话框）仍由页面壳持有——
 * 这里只把「点了哪一行」传回去，不复制一份状态。
 */
export function KeyRow({
  item,
  window,
  confirming,
  onRemove,
  onCancelDelete,
  onEdit,
  onEditQuota,
  onImport,
  onConfirmDelete,
}: {
  item: ApiKey;
  /** 用量窗口（today / 7d / 30d）：仅影响「用量」列文案。 */
  window: ApiKeyWindow;
  confirming: boolean;
  onRemove: (id: string) => void;
  onCancelDelete: () => void;
  onEdit: (key: ApiKey) => void;
  onEditQuota: (key: ApiKey) => void;
  onImport: (key: ApiKey) => void;
  onConfirmDelete: (id: string) => void;
}) {
  return (
    <TableRow key={item.id}>
      <TableCell label="名称">{item.name || "—"}</TableCell>
      <TableCell label="Key" className="font-mono text-xs">{item.preview}</TableCell>
      <TableCell label="渠道" className="text-xs" data-testid={`key-binding-${item.id}`}>
        {providerBindingLabel(item.provider_binding)}
      </TableCell>
      <TableCell label="到期" className="text-xs tabular-nums" data-testid={`key-expires-${item.id}`}>
        {item.expires_at ? formatTime(item.expires_at) : "永不过期"}
      </TableCell>
      <TableCell label="最后使用" className="text-xs tabular-nums">
        {item.last_used_at ? formatTime(item.last_used_at) : "从未使用"}
      </TableCell>
      <TableCell label="来源 IP" className="font-mono text-xs" data-testid={`key-ips-${item.id}`}>
        {item.allowed_ips || "不限制"}
      </TableCell>
      <TableCell label="模型白名单" className="font-mono text-xs" data-testid={`key-models-${item.id}`}>
        {item.allowed_models || "不限制"}
      </TableCell>
      <TableCell label="配额" className="text-xs" data-testid={`key-quota-${item.id}`}>
        {quotaSummary(quotaOf(item))}
      </TableCell>
      <TableCell label={`用量 · ${USAGE_WINDOW_LABELS[window]}`} className="text-xs"
                 data-testid={`key-usage-${item.id}`}>
        {usageLabel(usageOf(item), window)}
      </TableCell>
      <TableCell label="创建时间" className="text-xs tabular-nums">
        {formatTime(item.created_at)}
      </TableCell>
      <TableCell label="操作" className="text-right">
        {confirming ? (
          <span className="inline-flex gap-2">
            <Button size="sm" variant="danger" onClick={() => onRemove(item.id)}>
              确认删除
            </Button>
            <Button size="sm" variant="ghost" onClick={onCancelDelete}>
              取消
            </Button>
          </span>
        ) : (
          <span className="inline-flex flex-wrap justify-end gap-2">
            <Button
              size="sm"
              variant="ghost"
              data-testid={`edit-key-${item.id}`}
              onClick={() => onEdit(item)}
            >
              <Pencil className="mr-1 size-3.5" />
              编辑
            </Button>
            <Button
              size="sm"
              variant="ghost"
              data-testid={`edit-quota-${item.id}`}
              onClick={() => onEditQuota(item)}
            >
              <Gauge className="mr-1 size-3.5" />
              配额
            </Button>
            <Button
              size="sm"
              variant="ghost"
              data-testid={`import-ccswitch-${item.id}`}
              onClick={() => onImport(item)}
            >
              <Share2 className="mr-1 size-3.5" />
              导入 CC Switch
            </Button>
            <Button size="sm" variant="ghost" onClick={() => onConfirmDelete(item.id)}>
              <Trash2 className="mr-1 size-3.5" />
              删除
            </Button>
          </span>
        )}
      </TableCell>
    </TableRow>
  );
}
