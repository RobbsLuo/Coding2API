import { Gauge, KeyRound, Pencil, Plus, Share2, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { type ApiKeyWindow, updateApiKey, USAGE_WINDOWS, USAGE_WINDOW_LABELS } from "../api/apiKeys";
import { api } from "../api/client";
import { useApiKeys, useQueryClient } from "../api/hooks";
import { EMPTY_QUOTA, type KeyQuota, quotaInput, quotaOf, setApiKeyQuota } from "../api/keyQuota";
import type { ApiKey, ApiKeyCreated } from "../api/types";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { Pagination } from "../components/Pagination";
import { SortableHead } from "../components/SortableHead";
import { copyText } from "../lib/clipboard";
import { DEFAULT_PAGE_SIZE } from "../lib/constants";
import { errorText } from "../lib/errors";
import { useDialogFocus } from "../hooks/useDialogFocus";
import { useSort } from "../hooks/useSort";
import {
  Button,
  EmptyState,
  Field,
  Input,
  Notice,
  Panel,
  QueryErrorState,
  Select,
  Table,
  TableBody,
  TableHead,
  TableHeader,
  TableRow,
} from "../ui";
import { AnthropicEntry, OpenAIEntry } from "./apikeys/clientEntry";
import { CcSwitchImportButtons } from "./apikeys/ccSwitch";
import { KeyRow } from "./apikeys/KeyRow";
import { expiresAtInput, PROVIDER_BINDING_OPTIONS, parseExpiresAt } from "./apikeys/meta";
import { QuotaFields, QuotaSection } from "./apikeys/quota";
import { CopyButton } from "./apikeys/shared";

// 这些纯函数 / 组件原先是本文件的顶层导出，拆分后原样从这里再导出，
// 保持 `import { … } from "../ApiKeysPage"` 的既有调用方不变。
export {
  anthropicBaseUrl,
  anthropicExamples,
  openaiBaseUrl,
  openaiExamples,
} from "./apikeys/clientEntry";
export { CcSwitchImportButtons } from "./apikeys/ccSwitch";
export {
  PROVIDER_BINDING_OPTIONS,
  parseExpiresAt,
  providerBindingLabel,
} from "./apikeys/meta";

export function ApiKeysPage() {
  const { sort, order, toggle } = useSort("created_at", "asc", { last_used_at: "desc" });
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  // 列表用量窗口（今日 / 近 7 天 / 近 30 天）：切档 = 换数据集，回第一页
  const [usageWindow, setUsageWindow] = useState<ApiKeyWindow>("today");
  const { data, isLoading, isError, error: queryError, refetch, isFetching } = useApiKeys(
    undefined, sort, order, pageSize, page * pageSize, usageWindow);
  const client = useQueryClient();
  // 换排序 = 换数据集：回到第一页，避免停在越界页码上（空白）
  const toggleSort = (columnKey: string) => {
    toggle(columnKey);
    setPage(0);
  };
  const changePageSize = (size: number) => {
    setPageSize(size);
    setPage(0);
  };
  const changeWindow = (window: ApiKeyWindow) => {
    setUsageWindow(window);
    setPage(0);
  };
  const [name, setName] = useState("");
  const [binding, setBinding] = useState("");
  const [allowedIps, setAllowedIps] = useState("");
  const [allowedModels, setAllowedModels] = useState("");
  const [expiresAt, setExpiresAt] = useState("");
  const [quota, setQuota] = useState<KeyQuota>({ ...EMPTY_QUOTA });
  // 列表行「改配额」对话框：整体替换一把 Key 的配额
  const [editingQuota, setEditingQuota] = useState<ApiKey | null>(null);
  const [quotaDraft, setQuotaDraft] = useState<KeyQuota>({ ...EMPTY_QUOTA });
  // 列表行「编辑」对话框：按字段改名称 / 渠道绑定 / 两个白名单 / 到期时间
  const [editingKey, setEditingKey] = useState<ApiKey | null>(null);
  const [editName, setEditName] = useState("");
  const [editBinding, setEditBinding] = useState("");
  const [editIps, setEditIps] = useState("");
  const [editModels, setEditModels] = useState("");
  const [editExpiresAt, setEditExpiresAt] = useState("");
  const [savingEdit, setSavingEdit] = useState(false);
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // 创建请求进行中：按钮禁用 + 早退，避免连点重复 POST（每次都会生成不可回收的明文 Key）
  const [creating, setCreating] = useState(false);
  // 同帧连点读不到刚设的 state，用 ref 做真正的互斥
  const creatingRef = useRef(false);
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);
  // 创建 Key 收进对话框（低频动作不常驻占屏），由「我的 API Key」右上角触发
  // `?new=1`：首启向导的深链，进来直接开创建对话框，随后把参数清掉
  const [searchParams, setSearchParams] = useSearchParams();
  const [createOpen, setCreateOpen] = useState(() => searchParams.get("new") === "1");
  useEffect(() => {
    if (searchParams.get("new") === "1") setSearchParams({}, { replace: true });
  }, [searchParams, setSearchParams]);
  // 列表行「导入 CC Switch」对话框：列表只有脱敏 preview，明文由用户自己粘贴
  const [importing, setImporting] = useState<ApiKey | null>(null);
  const [importKey, setImportKey] = useState("");

  const keys = data?.api_keys ?? [];

  const create = async (event: React.FormEvent) => {
    event.preventDefault();
    // 连点保护：成功后返回的是一次性明文 Key（不可回收），重复 POST 会白造几把。
    // state 更新是异步的，同一帧内的第二次提交读到的 `creating` 仍是 false，
    // 故再用 ref 兜一层真正的互斥。
    if (creatingRef.current) return;
    creatingRef.current = true;
    setCreating(true);
    setError(null);
    try {
      const quotaPayload = quotaInput(quota);
      const result = await api.createApiKey({
        name,
        provider_binding: binding,
        allowed_ips: allowedIps.trim(),
        allowed_models: allowedModels.trim(),
        expires_at: parseExpiresAt(expiresAt),
        // 全 0（= 不限制）时不带该字段：保持与老后端/老请求体一致，
        // 也让「没配配额」与「显式传空配额」在日志里可区分。
        ...(quotaPayload ? { quota: quotaPayload } : {}),
      });
      setCreated(result);
      setName("");
      setBinding("");
      setAllowedIps("");
      setAllowedModels("");
      setExpiresAt("");
      setQuota({ ...EMPTY_QUOTA });
      setCopied(false);
      await client.invalidateQueries({ queryKey: ["admin"] });
    } catch (caught) {
      // 用 errorText 取后端受控文案（渠道绑定冲突、白名单格式错等都说得出
      // 具体哪里不对）；只有非受控异常才回落通用兜底。裸 catch{} 会把后端
      // 已经写好的原因丢掉，用户只能对着「创建失败」猜。
      setError(errorText(caught,
        "创建失败：请检查渠道绑定、模型白名单、IP 白名单与到期时间格式（配额须为非负数字）"));
    } finally {
      creatingRef.current = false;
      setCreating(false);
    }
  };

  const openQuotaEditor = (key: ApiKey) => {
    setEditingQuota(key);
    setQuotaDraft(quotaOf(key));
    setError(null);
  };

  const saveQuota = async () => {
    if (!editingQuota) return;
    setError(null);
    try {
      await setApiKeyQuota(editingQuota.id, quotaDraft);
      setEditingQuota(null);
      await client.invalidateQueries({ queryKey: ["admin"] });
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "保存配额失败");
    }
  };

  const openEdit = (key: ApiKey) => {
    setEditingKey(key);
    setEditName(key.name);
    setEditBinding(key.provider_binding);
    setEditIps(key.allowed_ips);
    setEditModels(key.allowed_models);
    setEditExpiresAt(expiresAtInput(key.expires_at));
    setError(null);
  };

  const closeEdit = () => {
    setEditingKey(null);
    setError(null);
  };

  const saveEdit = async () => {
    // 保存中早退：PUT 是幂等的，但连点会叠出多条「值相同」的审计判定
    if (!editingKey || savingEdit) return;
    setSavingEdit(true);
    setError(null);
    try {
      // 五个字段全量提交：未改的字段值与原值相同，服务端只按**实际差异**
      // 写库与审计（`changed` 为空不发记录），因此不需要前端算 diff。
      await updateApiKey(editingKey.id, {
        name: editName,
        provider_binding: editBinding,
        allowed_ips: editIps.trim(),
        allowed_models: editModels.trim(),
        expires_at: parseExpiresAt(editExpiresAt),
      });
      setEditingKey(null);
      await client.invalidateQueries({ queryKey: ["admin"] });
    } catch (caught) {
      setError(errorText(caught, "保存失败：请检查渠道绑定、模型白名单与 IP 白名单格式"));
    } finally {
      setSavingEdit(false);
    }
  };

  const remove = async (id: string) => {
    setError(null);
    try {
      await api.deleteApiKey(id);
      setConfirming(null);
      await client.invalidateQueries({ queryKey: ["admin"] });
    } catch (caught) {
      // 裸 catch 会把后端已经写好的原因（不存在 / 权限不足）丢掉
      setError(errorText(caught, "删除失败"));
    }
  };

  const copy = async () => {
    if (!created) return;
    const ok = await copyText(created.api_key);
    setCopied(ok);
    // 复制的是一次性明文 Key，失败必须说清楚，否则关掉就再也拿不回来
    setCopyFailed(!ok);
  };

  const closeCreate = () => {
    setCreateOpen(false);
    setError(null);
    // 明文 Key 只应存在于创建确认弹窗的生命周期内（L3）：关闭即从 state 抹掉，
    // 否则它会继续留在 OpenAI/Anthropic 接入面板与页面内存里，与「只显示一次」不符。
    setCreated(null);
    setCopied(false);
    setCopyFailed(false);
  };

  const createDialogRef = useDialogFocus<HTMLDivElement>(closeCreate, createOpen);

  const closeImport = () => {
    setImporting(null);
    // 用户粘贴的明文 Key 只应存在于导入对话框的生命周期内，关闭即抹掉
    setImportKey("");
  };
  const importDialogRef = useDialogFocus<HTMLDivElement>(closeImport, importing !== null);

  const closeQuota = () => setEditingQuota(null);
  // 配额对话框也要焦点管理与 Esc 关闭（MASTER §6「对话框焦点管理与 Esc 关闭」）：
  // 此前只有「创建」「导入」两个对话框接了 useDialogFocus，配额这个漏了。
  const quotaDialogRef = useDialogFocus<HTMLDivElement>(closeQuota, editingQuota !== null);
  const editDialogRef = useDialogFocus<HTMLDivElement>(closeEdit, editingKey !== null);

  return (
    <div className="space-y-6" data-testid="api-keys-page">
      <PageHeader
        eyebrow="控制台"
        title="API Key 管理"
        description="创建外部客户端（ChatGPT Next Web、LobeChat、Cursor 等）接入用的 Key，协议与 OpenAI 兼容。Key 仅在创建时完整显示一次，请立即保存；用量按 Key 归属用户统计。"
        icon={<KeyRound className="size-5" />}
      />
      <OpenAIEntry apiKey={created?.api_key ?? ""} />
      <AnthropicEntry apiKey={created?.api_key ?? ""} />

      <Panel
        title="我的 API Key"
        action={
          <div className="flex flex-wrap items-center justify-end gap-2">
            {/* 用量窗口切换：三档对应服务端 `window` 参数（今日 / 近 7 天 / 近 30 天） */}
            <div
              className="inline-flex rounded-lg border border-border p-0.5"
              role="group"
              aria-label="用量窗口"
            >
              {USAGE_WINDOWS.map((option) => (
                <Button
                  key={option.value}
                  size="sm"
                  variant={usageWindow === option.value ? "primary" : "ghost"}
                  aria-pressed={usageWindow === option.value}
                  data-testid={`usage-window-${option.value}`}
                  onClick={() => changeWindow(option.value)}
                >
                  {option.label}
                </Button>
              ))}
            </div>
            <Button
              size="sm"
              variant="primary"
              data-testid="open-create-key-dialog"
              onClick={() => setCreateOpen(true)}
            >
              <Plus className="size-4" /> 创建 API Key
            </Button>
          </div>
        }
      >
        {/* 删除 / 配额 / 编辑等列表内操作的失败提示：各对话框都没开时也要有落点，
            否则后端写好的原因（不存在 / 权限不足）根本没人看得到 */}
        {error && !createOpen && !editingQuota && !editingKey && (
          <div className="mb-3" role="alert" data-testid="api-keys-error">
            <Notice tone="danger">{error}</Notice>
          </div>
        )}
        {isLoading ? (
          <PageSkeleton variant="table" rows={4} />
        ) : isError ? (
          <QueryErrorState
            title="API Key 列表加载失败"
            description={errorText(queryError, "拿不到 API Key 列表，请稍后重试。")}
            onRetry={() => void refetch()}
            data-testid="api-keys-error-state"
          />
        ) : keys.length === 0 ? (
          <EmptyState
            icon={<KeyRound className="size-5" />}
            title="还没有 API Key"
            description="点右上角「创建 API Key」生成一个，复制到外部客户端即可接入。"
            data-testid="no-keys"
          />
        ) : (
          <>
          <Table data-testid="keys-table" cards>
            <TableHeader>
              <TableRow>
                <SortableHead label="名称" columnKey="name" active={sort === "name"}
                              direction={order} onToggle={toggleSort} testId="sort-name" />
                <TableHead>Key</TableHead>
                <SortableHead label="渠道" columnKey="provider_binding"
                              active={sort === "provider_binding"} direction={order}
                              onToggle={toggleSort} testId="sort-provider_binding" />
                <SortableHead label="到期" columnKey="expires_at" active={sort === "expires_at"}
                              direction={order} onToggle={toggleSort} testId="sort-expires_at" />
                <SortableHead label="最后使用" columnKey="last_used_at"
                              active={sort === "last_used_at"} direction={order}
                              onToggle={toggleSort} testId="sort-last_used_at" />
                <TableHead>来源 IP</TableHead>
                <TableHead>模型白名单</TableHead>
                <TableHead>配额</TableHead>
                <TableHead>用量 · {USAGE_WINDOW_LABELS[usageWindow]}</TableHead>
                <SortableHead label="创建时间" columnKey="created_at" active={sort === "created_at"}
                              direction={order} onToggle={toggleSort} testId="sort-created_at" />
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {keys.map((key) => (
                <KeyRow
                  key={key.id}
                  item={key}
                  window={usageWindow}
                  confirming={confirming === key.id}
                  onRemove={remove}
                  onCancelDelete={() => setConfirming(null)}
                  onEdit={openEdit}
                  onEditQuota={openQuotaEditor}
                  onImport={setImporting}
                  onConfirmDelete={setConfirming}
                />
              ))}
            </TableBody>
          </Table>
          <Pagination
            testId="keys"
            page={page}
            pageSize={pageSize}
            total={data?.total ?? 0}
            busy={isFetching}
            onPageChange={setPage}
            onPageSizeChange={changePageSize}
          />
          </>
        )}
      </Panel>

      {/* 创建 Key 对话框：创建成功后仍停留在对话框内展示一次性明文，由用户主动关闭 */}
      {createOpen && (
        <div
          className="fixed inset-0 z-50 grid place-items-center bg-black/50 p-4 backdrop-blur-sm"
          onClick={closeCreate}
        >
          <div
            ref={createDialogRef}
            role="dialog"
            aria-modal="true"
            aria-label="创建 API Key"
            data-testid="create-key-dialog"
            onClick={(event) => event.stopPropagation()}
            className="max-h-[90vh] w-full max-w-lg animate-in overflow-y-auto rounded-xl border border-border bg-card p-5 shadow-xl fade-in-0 zoom-in-95"
          >
            <div className="mb-4 flex items-center justify-between">
              <h2 className="flex items-center gap-2 text-sm font-semibold">
                <Plus className="size-4 text-muted-foreground" />
                创建 API Key
              </h2>
              <Button
                size="icon"
                variant="ghost"
                aria-label="关闭"
                data-testid="create-key-close"
                onClick={closeCreate}
              >
                <X className="size-4" />
              </Button>
            </div>

            {created ? (
              <div aria-live="polite">
                <Notice tone="warn">此 Key 只会显示这一次，请立即复制保存；关闭后无法再次查看。</Notice>
                <div className="mt-3 flex items-center gap-3">
                  <code
                    data-testid="new-key-plaintext"
                    className="flex-1 break-all rounded-lg border border-input bg-muted/40 px-3 py-2 font-mono text-xs"
                  >
                    {created.api_key}
                  </code>
                  <CopyButton label={copied ? "已复制" : "复制"} onCopy={copy} />
                  <Button size="sm" variant="ghost" onClick={closeCreate}>
                    我已保存
                  </Button>
                </div>
                {copyFailed && (
                  <p className="mt-2 text-xs text-danger" data-testid="new-key-copy-failed">
                    浏览器不允许自动复制，请手动选中上方 Key 复制后再关闭。
                  </p>
                )}
                {/* 导入按钮与明文 Key 同生命周期：只在这里（对话框开着时）才有可用的深链 */}
                <div className="mt-4 border-t border-border/70 pt-4">
                  <CcSwitchImportButtons
                    apiKey={created.api_key}
                    warning="深链含 API Key 明文，勿分享、勿在不可信环境使用；关闭对话框后不再保留。"
                  />
                </div>
              </div>
            ) : (
              <form onSubmit={create} className="space-y-3">
                <Field label="名称（可选）" htmlFor="key-name-input">
                  <Input
                    id="key-name-input"
                    value={name}
                    data-testid="key-name"
                    placeholder="例如 laptop"
                    onChange={(event) => setName(event.target.value)}
                  />
                </Field>
                <Field label="渠道绑定" htmlFor="key-binding-select">
                  <Select
                    id="key-binding-select"
                    value={binding}
                    data-testid="key-binding"
                    onChange={(event) => setBinding(event.target.value)}
                  >
                    {PROVIDER_BINDING_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </Select>
                </Field>
                <Field label="来源 IP 白名单（逗号分隔 IP/CIDR，留空不限制）" htmlFor="key-allowed-ips-input">
                  <Input
                    id="key-allowed-ips-input"
                    value={allowedIps}
                    data-testid="key-allowed-ips"
                    placeholder="例如 203.0.113.9,10.0.0.0/8"
                    onChange={(event) => setAllowedIps(event.target.value)}
                  />
                </Field>
                <Field label="模型白名单（逗号分隔模型名或 glob，留空不限制）" htmlFor="key-allowed-models-input">
                  <Input
                    id="key-allowed-models-input"
                    value={allowedModels}
                    data-testid="key-allowed-models"
                    placeholder="例如 glm-*,kimi-k3"
                    onChange={(event) => setAllowedModels(event.target.value)}
                  />
                </Field>
                <Field label="到期时间（留空永不过期）" htmlFor="key-expires-at-input">
                  <Input
                    id="key-expires-at-input"
                    type="datetime-local"
                    value={expiresAt}
                    data-testid="key-expires-at"
                    onChange={(event) => setExpiresAt(event.target.value)}
                  />
                </Field>
                <QuotaSection title="配额（可选）" value={quota} onChange={setQuota}
                              testid="key-quota" />
                {/* 创建结果（成功/失败）无焦点变化，aria-live 让读屏可感知 */}
                <div aria-live="polite">
                  {error && (
                    <div role="alert">
                      <Notice tone="danger">{error}</Notice>
                    </div>
                  )}
                </div>
                <div className="flex justify-end gap-2">
                  <Button type="button" variant="ghost" onClick={closeCreate}>
                    取消
                  </Button>
                  <Button type="submit" variant="primary" disabled={creating}
                          data-testid="create-key-submit">
                    <Plus className="mr-1 size-4" />
                    {creating ? "创建中…" : "创建"}
                  </Button>
                </div>
              </form>
            )}
          </div>
        </div>
      )}

      {/* 列表行「配额」对话框：整体替换该 Key 的配额（全 0 = 清除） */}
      {editingQuota && (
        <div
          className="fixed inset-0 z-50 grid place-items-center bg-black/50 p-4 backdrop-blur-sm"
          onClick={() => setEditingQuota(null)}
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-label="设置 Key 配额"
            data-testid="key-quota-dialog"
            ref={quotaDialogRef}
            onClick={(event) => event.stopPropagation()}
            className="max-h-[90vh] w-full max-w-lg animate-in overflow-y-auto rounded-xl border border-border bg-card p-5 shadow-xl fade-in-0 zoom-in-95"
          >
            <div className="mb-4 flex items-center justify-between">
              <h2 className="flex items-center gap-2 text-sm font-semibold">
                <Gauge className="size-4 text-muted-foreground" />
                设置配额 · {editingQuota.name || editingQuota.preview}
              </h2>
              <Button
                size="icon"
                variant="ghost"
                aria-label="关闭"
                data-testid="key-quota-close"
                onClick={() => setEditingQuota(null)}
              >
                <X className="size-4" />
              </Button>
            </div>
            <div className="space-y-3">
              <QuotaFields value={quotaDraft} onChange={setQuotaDraft}
                           testidPrefix="key-quota-edit" />
              <p className="text-xs text-muted-foreground">
                全部填 0 = 清除配额（恢复不限制）。保存后下一个请求立即生效。
              </p>
              <div aria-live="polite">
                {error && (
                  <div role="alert">
                    <Notice tone="danger">{error}</Notice>
                  </div>
                )}
              </div>
              <div className="flex justify-end gap-2">
                <Button variant="ghost" onClick={() => setEditingQuota(null)}>
                  取消
                </Button>
                <Button variant="primary" data-testid="key-quota-save" onClick={() => void saveQuota()}>
                  保存
                </Button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* 列表行「编辑」对话框：名称 / 渠道绑定 / 两个白名单 / 到期时间（明文与 id 不可改） */}
      {editingKey && (
        <div
          className="fixed inset-0 z-50 grid place-items-center bg-black/50 p-4 backdrop-blur-sm"
          onClick={closeEdit}
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-label="编辑 API Key"
            data-testid="edit-key-dialog"
            ref={editDialogRef}
            onClick={(event) => event.stopPropagation()}
            className="max-h-[90vh] w-full max-w-lg animate-in overflow-y-auto rounded-xl border border-border bg-card p-5 shadow-xl fade-in-0 zoom-in-95"
          >
            <div className="mb-4 flex items-center justify-between">
              <h2 className="flex items-center gap-2 text-sm font-semibold">
                <Pencil className="size-4 text-muted-foreground" />
                编辑 · {editingKey.name || editingKey.preview}
              </h2>
              <Button
                size="icon"
                variant="ghost"
                aria-label="关闭"
                data-testid="edit-key-close"
                onClick={closeEdit}
              >
                <X className="size-4" />
              </Button>
            </div>
            <form
              className="space-y-3"
              onSubmit={(event) => {
                event.preventDefault();
                void saveEdit();
              }}
            >
              <Field label="名称（可选）" htmlFor="edit-key-name-input">
                <Input
                  id="edit-key-name-input"
                  value={editName}
                  data-testid="edit-key-name"
                  placeholder="例如 laptop"
                  onChange={(event) => setEditName(event.target.value)}
                />
              </Field>
              <Field label="渠道绑定" htmlFor="edit-key-binding-select">
                <Select
                  id="edit-key-binding-select"
                  value={editBinding}
                  data-testid="edit-key-binding"
                  onChange={(event) => setEditBinding(event.target.value)}
                >
                  {PROVIDER_BINDING_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label="来源 IP 白名单（逗号分隔 IP/CIDR，留空不限制）"
                     htmlFor="edit-key-allowed-ips-input">
                <Input
                  id="edit-key-allowed-ips-input"
                  value={editIps}
                  data-testid="edit-key-allowed-ips"
                  placeholder="例如 203.0.113.9,10.0.0.0/8"
                  onChange={(event) => setEditIps(event.target.value)}
                />
              </Field>
              <Field label="模型白名单（逗号分隔模型名或 glob，留空不限制）"
                     htmlFor="edit-key-allowed-models-input">
                <Input
                  id="edit-key-allowed-models-input"
                  value={editModels}
                  data-testid="edit-key-allowed-models"
                  placeholder="例如 glm-*,kimi-k3"
                  onChange={(event) => setEditModels(event.target.value)}
                />
              </Field>
              <Field label="到期时间（留空永不过期）" htmlFor="edit-key-expires-at-input">
                <Input
                  id="edit-key-expires-at-input"
                  type="datetime-local"
                  value={editExpiresAt}
                  data-testid="edit-key-expires-at"
                  onChange={(event) => setEditExpiresAt(event.target.value)}
                />
              </Field>
              <p className="text-xs text-muted-foreground">
                明文 Key 与 id 不可修改；改完下一个请求立即按新策略鉴权。配额请用「配额」按钮。
              </p>
              <div aria-live="polite">
                {error && (
                  <div role="alert">
                    <Notice tone="danger">{error}</Notice>
                  </div>
                )}
              </div>
              <div className="flex justify-end gap-2">
                <Button type="button" variant="ghost" onClick={closeEdit}>
                  取消
                </Button>
                <Button type="submit" variant="primary" disabled={savingEdit}
                        data-testid="edit-key-save">
                  {savingEdit ? "保存中…" : "保存"}
                </Button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* 列表行「导入 CC Switch」对话框：明文 Key 由用户自己粘贴（列表只有脱敏 preview） */}
      {importing && (
        <div
          className="fixed inset-0 z-50 grid place-items-center bg-black/50 p-4 backdrop-blur-sm"
          onClick={closeImport}
        >
          <div
            ref={importDialogRef}
            role="dialog"
            aria-modal="true"
            aria-label="导入到 CC Switch"
            data-testid="ccswitch-row-dialog"
            onClick={(event) => event.stopPropagation()}
            className="max-h-[90vh] w-full max-w-lg animate-in overflow-y-auto rounded-xl border border-border bg-card p-5 shadow-xl fade-in-0 zoom-in-95"
          >
            <div className="mb-4 flex items-center justify-between">
              <h2 className="flex items-center gap-2 text-sm font-semibold">
                <Share2 className="size-4 text-muted-foreground" />
                导入到 CC Switch
              </h2>
              <Button
                size="icon"
                variant="ghost"
                aria-label="关闭"
                data-testid="ccswitch-row-close"
                onClick={closeImport}
              >
                <X className="size-4" />
              </Button>
            </div>
            <div className="space-y-3">
              <p className="text-xs text-muted-foreground">
                本列表只保留脱敏预览（{importing.preview}），服务端不再存明文，故需
                粘贴创建时保存的完整 Key 才能生成导入深链（CC Switch v4.0.10+）。
              </p>
              <Field label="API Key" htmlFor="ccswitch-row-key-input" hint="仅本次导入的内存中使用，关闭即清除，不落库、不上传。">
                <Input
                  id="ccswitch-row-key-input"
                  value={importKey}
                  data-testid="ccswitch-row-key"
                  placeholder="sk-..."
                  autoComplete="off"
                  onChange={(event) => setImportKey(event.target.value)}
                />
              </Field>
              <CcSwitchImportButtons
                apiKey={importKey}
                testidPrefix="ccswitch-row"
                warning="深链含粘贴的 API Key 明文，勿分享、勿在不可信环境使用；关闭对话框后不再保留。"
              />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
