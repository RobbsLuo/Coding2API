import { RotateCcw, Save, SlidersHorizontal } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { api } from "../api/client";
import { formatAgo, formatDuration, taskReportLabel } from "../api/display";
import { useQueryClient, useSettings, useTasks } from "../api/hooks";
import { useSessionContext } from "../Layout";
import type { RuntimeSetting, TaskStatus } from "../api/types";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { Badge, Button, Card, EmptyState, Field, Input, Notice, Panel, Select, Tabs } from "../ui";

/** 网关卡在 tabs 里的值：加前缀避免与任务 key 撞车（如任务与分组同名）。 */
const groupTabValue = (key: string) => `gateway-${key}`;

/** 兜底 tab：归属对不上（task 指向未装配任务、后端留空 group）的项都进这里。 */
const OTHER_TAB = "settings-other";

/** tab 文案：去掉任务名里的括号补充（如「模型目录刷新（OpenRouter）」），
 * 十几个 tab 排一行时它会把行撑爆、挤得每个标签折行；完整名仍在卡片标题与告警里。 */
function tabLabel(name: string): string {
  return name.replace(/（[^）]*）/g, "").trim() || name;
}

/** 个别仍偏长的任务名在 tab 上用简称（显示用，不影响卡片标题/告警/接口字段）。 */
const TAB_SHORT_NAMES: Record<string, string> = {
  model_catalog: "渠道模型刷新",
};

/** tab 文案：优先显式简称，其次去掉括号补充，最后回落到原任务名。 */
function tabText(task: { key: string; name: string }): string {
  return TAB_SHORT_NAMES[task.key] ?? tabLabel(task.name);
}

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
            {/* 输入控件的可读名称：Field 的 label 未与控件 id 关联，读屏拿不到「生效值」属于哪一项 */}
            <Field label={`${setting.label} 生效值`}>
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

/**
 * 一个后台任务卡片：运行态在上，该任务的可热更配置在下。
 *
 * 归属关系由后端下发（`RuntimeSetting.task`），前端不硬编码 key 列表——
 * 后端加任务/改归属不需要动这里。
 */
function TaskCard({
  task,
  serverTime,
  rows,
}: {
  task: TaskStatus;
  serverTime: number;
  rows: ReactNode[];
}) {
  const tone = task.last_error ? "danger" : task.last_ok ? "ok" : "muted";
  const stateLabel = !task.enabled
    ? "已关闭"
    : task.last_error
      ? "上次失败"
      : task.last_ok
        ? "正常"
        : "尚未执行";
  return (
    <Card
      className="mb-3 gap-0 p-4 last:mb-0"
      data-testid={`task-${task.key}`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium">{task.name}</span>
        <Badge tone={task.enabled ? tone : "muted"}>{stateLabel}</Badge>
        <span
          className="text-xs text-muted-foreground"
          data-testid={`task-interval-${task.key}`}
        >
          周期 {formatDuration(task.interval_seconds)}
        </span>
        <span
          className="text-xs text-muted-foreground"
          data-testid={`task-last-${task.key}`}
        >
          {task.last_finished_at
            ? `上次执行 ${formatAgo(task.last_finished_at, serverTime)}`
            : "本进程内尚未执行"}
        </span>
        {task.runs > 0 && (
          <span className="text-xs text-muted-foreground">已跑 {task.runs} 轮</span>
        )}
      </div>
      <p className="mt-1 text-xs text-muted-foreground">{task.description}</p>
      {task.last_error && (
        <p className="mt-1 text-xs text-destructive" data-testid={`task-error-${task.key}`}>
          最近失败：{task.last_error}
        </p>
      )}
      {task.last_report && (
        <p className="mt-1 text-xs text-muted-foreground" data-testid={`task-report-${task.key}`}>
          最近结果：{taskReportLabel(task.last_report)}
        </p>
      )}
      {rows.length > 0 ? (
        <div className="mt-2">{rows}</div>
      ) : (
        <p className="mt-2 text-xs text-muted-foreground" data-testid={`task-noconfig-${task.key}`}>
          无可热更配置（周期固定，见上方说明）。
        </p>
      )}
    </Card>
  );
}

export function SettingsPage() {
  const session = useSessionContext();
  const { data, isLoading } = useSettings(session.username);
  const tasksQuery = useTasks(session.username);
  const client = useQueryClient();
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [activeTab, setActiveTab] = useState<string | null>(null);

  const settings = useMemo(() => data?.settings ?? [], [data]);
  const tasks = useMemo(() => tasksQuery.data?.tasks ?? [], [tasksQuery.data]);
  const serverTime = tasksQuery.data?.server_time ?? Date.now() / 1000;

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

  // 一屏只展示一块：每个后台任务一个 tab（运行态 + 自己的配置），无任务归属的配置
  // 按后端下发的网关卡组各占一个 tab。归属对不上的项落进「其他」，绝不吞掉配置。
  const groups = useMemo(() => data?.groups ?? [], [data]);
  const taskKeys = new Set(tasks.map((task) => task.key));
  const assignTab = (setting: RuntimeSetting) => {
    if (setting.task && taskKeys.has(setting.task)) return setting.task;
    if (groups.some((group) => group.value === setting.group)) {
      return groupTabValue(setting.group as string);
    }
    return OTHER_TAB;
  };

  const tabOptions = [
    ...tasks.map((task) => ({ value: task.key, label: tabText(task) })),
    // 只列出「确实有配置项」的网关卡组，避免点开是空页
    ...groups
      .filter((group) => settings.some((s) => assignTab(s) === groupTabValue(group.value)))
      .map((group) => ({ value: groupTabValue(group.value), label: group.label })),
  ];
  if (settings.some((setting) => assignTab(setting) === OTHER_TAB)) {
    tabOptions.push({ value: OTHER_TAB, label: "其他" });
  }

  const active =
    activeTab && tabOptions.some((option) => option.value === activeTab)
      ? activeTab
      : tabOptions[0]?.value;
  const activeTask = tasks.find((task) => task.key === active);
  const activeGroupLabel =
    tabOptions.find((option) => option.value === active)?.label ?? "未归属配置";
  const activeRows = settings.filter((setting) => assignTab(setting) === active);

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
      // 黑名单/默认模型改了要立刻反映到 Playground：那里自带列表缓存
      // （queryKey ["playground-models"]，TTL 内不会重取），不失效会显示旧列表。
      await client.invalidateQueries({ queryKey: ["playground-models"] });
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

  const renderSetting = (setting: RuntimeSetting) => (
    <SettingRow
      key={setting.key}
      setting={setting}
      draft={drafts[setting.key] ?? toInputValue(setting)}
      disabled={busy}
      onChange={(value) => setDrafts((previous) => ({ ...previous, [setting.key]: value }))}
      onReset={() => void reset(setting)}
    />
  );

  return (
    <div className="space-y-6" data-testid="settings-page">
      <PageHeader
        eyebrow="管理"
        title="任务与配置"
        description="后台任务最近跑得怎么样、下一次什么时候跑，和它自己的周期/开关放在一起；这里改的值存进数据库并立即生效，无需重启。"
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
          <div className="mb-3" data-testid="settings-error" role="alert">
            <Notice tone="danger">{error}</Notice>
          </div>
        )}
        {notice && (
          <div className="mb-3" data-testid="settings-notice" role="status">
            <Notice tone="ok">{notice}</Notice>
          </div>
        )}
        {invalid.length > 0 && (
          <div className="mb-3" data-testid="settings-invalid" role="alert">
            <Notice tone="danger">
              有 {invalid.length} 项填写不合法（不能为空或非数字），请先修正。
            </Notice>
          </div>
        )}
        {isLoading ? (
          <PageSkeleton variant="table" rows={5} />
        ) : settings.length === 0 ? (
          <EmptyState
            icon={<SlidersHorizontal className="size-5" />}
            title="没有可热更配置"
            description="当前没有可在线调整的配置项。"
            data-testid="no-settings"
          />
        ) : (
          <>
            {!tasksQuery.isLoading && tasks.length === 0 && (
              <p className="mb-4 text-xs text-muted-foreground" data-testid="no-tasks">
                拿不到任务运行态（调度尚未启动或接口不可用）；下面按配置项直接列出。
              </p>
            )}

            {tasksQuery.isLoading ? (
              <PageSkeleton variant="table" rows={4} />
            ) : (
              <>
                <div className="mb-4 overflow-x-auto pb-1">
                  <Tabs
                    value={active ?? ""}
                    options={tabOptions}
                    onChange={setActiveTab}
                    testId="settings-tabs"
                    compact
                  />
                </div>

                {activeTask ? (
                  <TaskCard
                    task={activeTask}
                    serverTime={serverTime}
                    rows={activeRows.map(renderSetting)}
                  />
                ) : (
                  <>
                    <h3 className="text-xs font-semibold tracking-wide text-muted-foreground">
                      {activeGroupLabel}
                    </h3>
                    {activeRows.length > 0 ? (
                      activeRows.map(renderSetting)
                    ) : (
                      <p className="text-xs text-muted-foreground" data-testid="no-gateway-settings">
                        这里没有配置项。
                      </p>
                    )}
                  </>
                )}
              </>
            )}
          </>
        )}
      </Panel>
    </div>
  );
}
