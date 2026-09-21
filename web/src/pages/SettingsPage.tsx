import { RotateCcw, Save, SlidersHorizontal } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { useQueryClient, useSettings } from "../api/hooks";
import { useSessionContext } from "../Layout";
import type { RuntimeSetting } from "../api/types";
import { PageHeader } from "../components/PageHeader";
import { Badge, Button, Empty, Field, Input, Notice, Panel, Select } from "../ui";

/** 前端只做「文本 → 待提交标量」的粗转；范围/组合校验以后端为准。 */
function toInputValue(setting: RuntimeSetting): string {
  return String(setting.value);
}

function toSubmitValue(setting: RuntimeSetting, raw: string): unknown {
  if (setting.kind === "bool") return raw === "true";
  if (setting.kind === "int") return Number.parseInt(raw, 10);
  if (setting.kind === "float") return Number.parseFloat(raw);
  return raw;
}

/** 提交前的基本校验：拦住空值与非数字，避免把 NaN 发往后端。 */
function isSubmittable(setting: RuntimeSetting, raw: string): boolean {
  if (setting.kind === "str") return raw.trim() !== "";
  if (setting.kind === "bool") return raw === "true" || raw === "false";
  const parsed = Number(raw);
  return raw.trim() !== "" && Number.isFinite(parsed);
}

/** 一行配置：左侧文案 + 输入控件 + 来源标记，右侧恢复默认。 */
function SettingRow({
  setting,
  draft,
  onChange,
  onReset,
  disabled,
}: {
  setting: RuntimeSetting;
  draft: string;
  onChange: (value: string) => void;
  onReset: () => void;
  disabled: boolean;
}) {
  const dirty = draft !== toInputValue(setting);
  return (
    <div
      className="border-b border-border py-4 last:border-b-0"
      data-testid={`setting-${setting.key}`}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium">{setting.label}</span>
            <span data-testid={`source-${setting.key}`}>
              <Badge tone={setting.overridden ? "accent" : "muted"}>
                {setting.overridden ? "DB 覆盖" : "来自 .env"}
              </Badge>
            </span>
            <code className="text-xs text-muted-foreground">{setting.env_name}</code>
          </div>
          <p className="mt-1 text-xs text-muted-foreground">{setting.description}</p>
        </div>
        <div className="flex items-end gap-2">
          <div className="w-44">
            <Field label="生效值">
              {setting.kind === "bool" ? (
                <Select
                  value={draft}
                  disabled={disabled}
                  data-testid={`input-${setting.key}`}
                  onChange={(event) => onChange(event.target.value)}
                >
                  <option value="true">开启</option>
                  <option value="false">关闭</option>
                </Select>
              ) : (
                <Input
                  value={draft}
                  disabled={disabled}
                  data-testid={`input-${setting.key}`}
                  onChange={(event) => onChange(event.target.value)}
                />
              )}
            </Field>
          </div>
          <Button
            size="sm"
            variant="ghost"
            disabled={disabled || !setting.overridden}
            data-testid={`reset-${setting.key}`}
            onClick={onReset}
          >
            <RotateCcw className="mr-1 size-3.5" />
            恢复默认
          </Button>
        </div>
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
        <span data-testid={`default-${setting.key}`}>
          .env 默认：{String(setting.default)}
        </span>
        {dirty && <span className="text-warn">未保存的修改</span>}
      </div>
    </div>
  );
}

export function SettingsPage() {
  const session = useSessionContext();
  const { data, isLoading } = useSettings(session.username);
  const client = useQueryClient();
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const settings = useMemo(() => data?.settings ?? [], [data]);

  // 服务端刷新后，未保存的草稿要保留；已被保存的项则回到服务端值。
  useEffect(() => {
    setDrafts((previous) => {
      const next: Record<string, string> = {};
      for (const setting of settings) {
        next[setting.key] = previous[setting.key] ?? toInputValue(setting);
      }
      return next;
    });
  }, [settings]);

  const dirty = settings.filter(
    (setting) => drafts[setting.key] !== undefined && drafts[setting.key] !== toInputValue(setting),
  );
  const invalid = dirty.filter((setting) => !isSubmittable(setting, drafts[setting.key]));

  const refresh = () => client.invalidateQueries({ queryKey: ["admin"] });

  const save = async () => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const values: Record<string, unknown> = {};
      for (const setting of dirty) {
        values[setting.key] = toSubmitValue(setting, drafts[setting.key]);
      }
      await api.updateSettings(values);
      setNotice(`已保存 ${dirty.length} 项，立即生效（DB 覆盖 .env）。`);
      setDrafts({});
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "保存失败");
    } finally {
      setBusy(false);
    }
  };

  const reset = async (setting: RuntimeSetting) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await api.updateSettings({ [setting.key]: null });
      setNotice(`「${setting.label}」已恢复 .env 默认值。`);
      setDrafts({});
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "恢复失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-6" data-testid="settings-page">
      <PageHeader
        title="运行时配置"
        description="这里改的值存进数据库并立即生效，无需重启；它覆盖 .env 里的同名项。「恢复默认」会删掉覆盖值，重新回落到 .env。"
        icon={<SlidersHorizontal className="size-5" />}
      />

      <Notice tone="warn">
        覆盖优先级：<strong>数据库覆盖值 &gt; .env</strong>。因此直接改 .env 对已被覆盖的项无效——
        请先在这里「恢复默认」，或直接改本页的值。启动期项（密钥、端口、数据目录、上游白名单）
        不在本页，改它们必须重启。
      </Notice>

      <Panel
        title={`可热更配置（${settings.length}）`}
        action={
          <div className="flex items-center gap-2">
            <span className="text-xs text-muted-foreground" data-testid="dirty-count">
              {dirty.length ? `${dirty.length} 项待保存` : "无改动"}
            </span>
            <Button
              size="sm"
              variant="primary"
              disabled={busy || dirty.length === 0 || invalid.length > 0}
              data-testid="save-settings"
              onClick={() => void save()}
            >
              <Save className="mr-1 size-3.5" />
              保存
            </Button>
          </div>
        }
      >
        {error && (
          <div className="mb-3" data-testid="settings-error">
            <Notice tone="danger">{error}</Notice>
          </div>
        )}
        {notice && (
          <div className="mb-3" data-testid="settings-notice">
            <Notice tone="ok">{notice}</Notice>
          </div>
        )}
        {invalid.length > 0 && (
          <div className="mb-3" data-testid="settings-invalid">
            <Notice tone="danger">
              有 {invalid.length} 项填写不合法（不能为空或非数字），请先修正。
            </Notice>
          </div>
        )}
        {isLoading ? (
          <Empty>载入中…</Empty>
        ) : settings.length === 0 ? (
          <Empty data-testid="no-settings">没有可热更配置</Empty>
        ) : (
          settings.map((setting) => (
            <SettingRow
              key={setting.key}
              setting={setting}
              draft={drafts[setting.key] ?? toInputValue(setting)}
              disabled={busy}
              onChange={(value) =>
                setDrafts((previous) => ({ ...previous, [setting.key]: value }))
              }
              onReset={() => void reset(setting)}
            />
          ))
        )}
      </Panel>
    </div>
  );
}
