import { useMemo, useRef, useState } from "react";
import { FileUp, TriangleAlert, Upload } from "lucide-react";
import { api, ApiError } from "../api/client";
import type { BatchImportEntry, BatchImportResult } from "../api/types";
import { useDialogFocus } from "../hooks/useDialogFocus";
import { Button, Notice } from "../ui";

type ParseError = { line: number; message: string };

/**
 * 批量导入文本 → 条目 + 逐行错误。
 *
 * 两种写法都收：
 * - JSON 数组：`[{provider, credential, nickname?, tags?}, …]`
 * - JSONL：一行一个对象（脚本导出/手工粘贴都常见）
 *
 * 解析与校验分开：某一行坏了只记该行，其余照常导入（后端也是同一口径）。
 * 行号从 1 开始，数组模式下用元素序号。
 */
export function parseBatchText(text: string): {
  entries: BatchImportEntry[];
  errors: ParseError[];
} {
  const trimmed = text.trim();
  if (!trimmed) return { entries: [], errors: [] };
  const entries: BatchImportEntry[] = [];
  const errors: ParseError[] = [];
  const accept = (raw: unknown, line: number) => {
    if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
      errors.push({ line, message: "必须是对象" });
      return;
    }
    const item = raw as Record<string, unknown>;
    if (typeof item.provider !== "string" || !item.provider.trim()) {
      errors.push({ line, message: "缺少 provider" });
      return;
    }
    if (typeof item.credential !== "object" || item.credential === null
        || Array.isArray(item.credential)) {
      errors.push({ line, message: "credential 必须是对象" });
      return;
    }
    entries.push({
      provider: item.provider.trim(),
      credential: item.credential,
      nickname: typeof item.nickname === "string" ? item.nickname : "",
      tags: Array.isArray(item.tags) ? item.tags.map(String) : [],
    });
  };

  if (trimmed.startsWith("[")) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(trimmed);
    } catch {
      return { entries: [], errors: [{ line: 1, message: "JSON 解析失败" }] };
    }
    if (!Array.isArray(parsed)) {
      return { entries: [], errors: [{ line: 1, message: "顶层必须是数组" }] };
    }
    parsed.forEach((raw, index) => accept(raw, index + 1));
    return { entries, errors };
  }

  trimmed.split(/\r?\n/).forEach((line, index) => {
    if (!line.trim()) return;
    try {
      accept(JSON.parse(line), index + 1);
    } catch {
      errors.push({ line: index + 1, message: "JSON 解析失败" });
    }
  });
  return { entries, errors };
}

/** 批量导入对话框：粘贴 / 选文件 → 预览 → 提交 → 失败行回填，便于重试。 */
export function BatchImportDialog({
  onClose,
  onImported,
  onNotice,
}: {
  onClose: () => void;
  onImported: (message: string) => void;
  onNotice: (message: string) => void;
}) {
  const dialogRef = useDialogFocus<HTMLDivElement>(onClose);
  const fileRef = useRef<HTMLInputElement>(null);
  const [text, setText] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<BatchImportResult | null>(null);
  const parsed = useMemo(() => parseBatchText(text), [text]);

  const readFile = async (file: File | undefined) => {
    if (!file) return;
    try {
      setText(await file.text());
      setError(null);
      setResult(null);
    } catch {
      setError("读取文件失败");
    }
  };

  const submit = async () => {
    if (!parsed.entries.length) {
      setError("没有可导入的条目");
      return;
    }
    setPending(true);
    setError(null);
    setResult(null);
    try {
      const response = await api.batchImportCredentials(parsed.entries);
      setResult(response);
      if (response.created.length) {
        onImported(`批量导入完成：成功 ${response.created.length} 条`);
      }
      if (response.failed.length) {
        // 失败行回填成 JSONL：用户可以改完直接再提交，不用重新粘贴全部
        setText(response.failed.map(({ index }) => JSON.stringify(parsed.entries[index])).join("\n"));
      } else if (response.skipped.length && !response.created.length) {
        onNotice(`全部为重复凭证（${response.skipped.length} 条），未新增`);
      }
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "批量导入失败，请稍后重试");
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/50 p-4 backdrop-blur-sm">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label="批量导入凭证"
        data-testid="batch-import-dialog"
        className="w-full max-w-2xl space-y-3 rounded-xl border border-border bg-card p-5 shadow-xl"
      >
        <div className="flex items-center gap-2">
          <Upload className="size-4 text-muted-foreground" />
          <h2 className="text-sm font-semibold">批量导入凭证</h2>
          <span className="ml-auto text-xs text-muted-foreground">最多 100 条</span>
        </div>
        <p className="text-xs text-muted-foreground">
          粘贴 JSON 数组或逐行 JSON（JSONL），每条形如
          <code className="mx-1">{"{provider, credential, nickname?, tags?}"}</code>。
          同渠道内容完全相同的凭证会自动跳过。
        </p>

        <div className="flex items-center gap-2">
          <input
            ref={fileRef}
            type="file"
            accept=".json,.jsonl,.txt"
            className="hidden"
            data-testid="batch-file"
            onChange={(event) => void readFile(event.target.files?.[0])}
          />
          <Button
            size="sm"
            variant="ghost"
            data-testid="batch-pick-file"
            onClick={() => fileRef.current?.click()}
          >
            <FileUp className="size-4" /> 选择文件
          </Button>
          <span className="text-xs text-muted-foreground" data-testid="batch-count">
            识别 {parsed.entries.length} 条
            {parsed.errors.length > 0 && `，${parsed.errors.length} 行有问题`}
          </span>
        </div>

        <textarea
          data-testid="batch-text"
          value={text}
          onChange={(event) => {
            setText(event.target.value);
            setResult(null);
          }}
          rows={8}
          spellCheck={false}
          placeholder={'{"provider":"codebuddy","credential":{"bearer_token":"…"}}'}
          className="w-full resize-y rounded-md border border-border bg-background p-2 font-mono text-xs"
        />

        {parsed.errors.length > 0 && (
          <div data-testid="batch-parse-errors" className="text-xs">
            <Notice tone="warn">
              {parsed.errors.slice(0, 5).map((item) => (
                <div key={item.line} className="inline-flex items-center gap-1.5">
                  <TriangleAlert className="size-3" />第 {item.line} 行：{item.message}
                </div>
              ))}
              {parsed.errors.length > 5 && <div>…另有 {parsed.errors.length - 5} 行</div>}
            </Notice>
          </div>
        )}

        {result && (
          <div data-testid="batch-result" className="text-xs">
            <Notice tone={result.failed.length ? "warn" : "ok"}>
              成功 {result.created.length} 条，跳过 {result.skipped.length} 条
              {result.failed.length > 0 && `，失败 ${result.failed.length} 条（已回填到输入框）`}
            </Notice>
          </div>
        )}

        {error && (
          <div data-testid="batch-error">
            <Notice tone="danger">{error}</Notice>
          </div>
        )}

        <div className="flex justify-end gap-2">
          <Button variant="ghost" data-testid="batch-cancel" onClick={onClose} disabled={pending}>
            关闭
          </Button>
          <Button
            variant="default"
            data-testid="batch-submit"
            onClick={() => void submit()}
            disabled={pending || parsed.entries.length === 0}
          >
            {pending ? "导入中…" : `导入 ${parsed.entries.length} 条`}
          </Button>
        </div>
      </div>
    </div>
  );
}
