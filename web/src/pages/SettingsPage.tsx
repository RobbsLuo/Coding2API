import { Play, RotateCcw, Save, SlidersHorizontal } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { api } from "../api/client";
import { formatAgo, formatDuration, taskReportLabel } from "../api/display";
import { errorText } from "../lib/errors";
import { useQueryClient, useRunTask, useSettings, useTasks } from "../api/hooks";
import { useSessionContext } from "../Layout";
import type { RuntimeSetting, TaskRunResult, TaskStatus } from "../api/types";
import { PageHeader } from "../components/PageHeader";
import { PageSkeleton } from "../components/PageSkeleton";
import { Badge, Button, Card, EmptyState, Field, Input, Notice, Panel, QueryErrorState, Select, Tabs } from "../ui";

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
            <Field label={`${setting.label} 生效值`} htmlFor={`input-${setting.key}`}>
              {setting.kind === "bool" ? (
                <Select
                  id={`input-${setting.key}`}
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
                  id={`input-${setting.key}`}
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
 *
 * 「立即运行」按钮只在卡片处于前台 tab 时存在；运行中禁用并按 `task.running`
 * （后端下发的「此刻有一轮在跑」）显示，避免连点打多轮上游。
 */
function TaskCard({
  task,
  serverTime,
  rows,
  running,
  result,
  onRun,
}: {
  task: TaskStatus;
  serverTime: number;
  rows: ReactNode[];
  running: boolean;
  result: TaskRunResult | null;
  onRun: () => void;
}) {
  const tone = task.last_error ? "danger" : task.last_ok ? "ok" : "muted";
  const stateLabel = !task.enabled
    ? "已关闭"
    : task.last_error
      ? "上次失败"
      : task.last_ok
        ? "正常"
        : "尚未执行";
  // 结果文案：区分「跑了」「没排上（已在跑）」「跑了但 no-op（被开关跳过）」。
  // 三种都叫「已运行」就是在谎报——尤其第三种，任务其实一步没动。
  const resultText = result
    ? result.busy
      ? "该任务正在运行，本次未重复触发"
      : result.ran
        ? result.last_ok === false
          ? `已运行，但本轮失败：${result.last_error ?? "未知原因"}`
          : `已运行一轮${result.last_report ? `：${taskReportLabel(result.last_report)}` : ""}`
        : "本轮未执行（任务已关闭或未到执行条件）"
    : null;
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
        <Button
          size="sm"
          variant="default"
          className="ml-auto"
          data-testid={`task-run-${task.key}`}
          // 关着的任务点了也是 no-op（后端会如实回 ran=false），直接禁用比
          // 「点了没反应」更诚实；运行中禁用则防连点。
          disabled={running || task.running || !task.enabled}
          title={!task.enabled ? "任务已关闭，先打开对应开关" : undefined}
          onClick={onRun}
        >
          <Play className="mr-1 size-3" />
          {task.running ? "运行中…" : "立即运行"}
        </Button>
      </div>
      <p className="mt-1 text-xs text-muted-foreground">{task.description}</p>
      {resultText && (
        <p
          className={`mt-1 text-xs ${result?.ran && result.last_ok === false ? "text-destructive" : "text-muted-foreground"}`}
          data-testid={`task-run-result-${task.key}`}
          role="status"
        >
          {resultText}
        </p>
      )}
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
  const { data, isLoading, isError, error: settingsError, refetch } =
    useSettings(session.username);
  const tasksQuery = useTasks(session.username);
  const runTask = useRunTask(session.username);
  const client = useQueryClient();
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // 两级 tab 的选择：一级（任务 / 配置）→ 二级（任务业务分组或网关卡组）→ 任务。
  // 三个 key 分开存：切走再切回时各自的二级选择还在。
  const [section, setSection] = useState<"tasks" | "gateway">("tasks");
  const [taskGroupKey, setTaskGroupKey] = useState<string | null>(null);
  const [gatewayGroupKey, setGatewayGroupKey] = useState<string | null>(null);
  const [taskKey, setTaskKey] = useState<string | null>(null);
  // 手动触发：正在跑的 key + 最近一次结果（按 key 存，切 tab 回来还在）
  const [runningKey, setRunningKey] = useState<string | null>(null);
  const [runResults, setRunResults] = useState<Record<string, TaskRunResult>>({});
  const [runError, setRunError] = useState<{ key: string; text: string } | null>(null);

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

  // 两级 tab：一级「后台任务 / 网关配置」；任务按后端下发的业务分组
  // （TASK_GROUPS）二级切换、组内每个任务一个 tab；配置按网关卡组二级切换。
  // 归属对不上的配置项落进「其他」，绝不吞掉配置。
  const groups = useMemo(() => data?.groups ?? [], [data]);
  const taskGroups = useMemo(() => tasksQuery.data?.groups ?? [], [tasksQuery.data]);
  const taskKeys = new Set(tasks.map((task) => task.key));
  const assignTab = (setting: RuntimeSetting) => {
    if (setting.task && taskKeys.has(setting.task)) return setting.task;
    if (groups.some((group) => group.value === setting.group)) {
      return groupTabValue(setting.group as string);
    }
    return OTHER_TAB;
  };

  const taskGroupOptions = taskGroups.filter((group) =>
    tasks.some((task) => task.group === group.value),
  );
  const gatewayGroupOptions = groups
    // 只列出「确实有配置项」的组，避免点开是空页
    .filter((group) => settings.some((s) => assignTab(s) === groupTabValue(group.value)))
    .map((group) => ({ value: groupTabValue(group.value), label: group.label }));
  if (settings.some((setting) => assignTab(setting) === OTHER_TAB)) {
    gatewayGroupOptions.push({ value: OTHER_TAB, label: "其他" });
  }

  // 只有两边都有内容时才显示一级 tab；缺一边就直接落到另一边。
  const hasSections = taskGroupOptions.length > 0 && gatewayGroupOptions.length > 0;
  const activeSection: "tasks" | "gateway" = hasSections
    ? section
    : taskGroupOptions.length > 0
      ? "tasks"
      : "gateway";

  const activeTaskGroup =
    taskGroupKey && taskGroupOptions.some((option) => option.value === taskGroupKey)
      ? taskGroupKey
      : taskGroupOptions[0]?.value;
  const taskGroupTasks = tasks.filter((task) => task.group === activeTaskGroup);
  const activeTask =
    taskGroupTasks.find((task) => task.key === taskKey) ?? taskGroupTasks[0];

  const activeGatewayGroup =
    gatewayGroupKey && gatewayGroupOptions.some((option) => option.value === gatewayGroupKey)
      ? gatewayGroupKey
      : gatewayGroupOptions[0]?.value;
  const activeRows =
    activeSection === "tasks" && activeTask
      ? settings.filter((setting) => setting.task === activeTask.key)
      : settings.filter((setting) => assignTab(setting) === activeGatewayGroup);

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
      setError(errorText(caught, "保存失败，请稍后重试。"));
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
      setError(errorText(caught, "恢复失败，请稍后重试。"));
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

  // 卡片内分节（后端下发 `section`）：19 项平铺一列太长，按「基础与迟滞 /
  // 规则阈值 …」切段；没有 section 的配置项照旧直排，不插空标题。
  const renderSettings = (rows: RuntimeSetting[]) => {
    const nodes: ReactNode[] = [];
    let current: string | null = null;
    for (const setting of rows) {
      if (setting.section && setting.section !== current) {
        nodes.push(
          <h4
            key={`section-${setting.key}`}
            className="mt-4 border-b border-border/70 pb-1 text-xs font-semibold tracking-wide text-muted-foreground first:mt-0"
            data-testid={`setting-section-${setting.key}`}
          >
            {setting.section}
          </h4>,
        );
      }
      current = setting.section;
      nodes.push(renderSetting(setting));
    }
    return nodes;
  };

  const triggerRun = async (key: string) => {
    setRunningKey(key);
    setRunError(null);
    try {
      const result = await runTask.mutateAsync(key);
      setRunResults((previous) => ({ ...previous, [key]: result }));
    } catch (caught) {
      setRunError({ key, text: errorText(caught, "触发失败，请稍后重试。") });
    } finally {
      setRunningKey(null);
    }
  };

  return (
    <div className="space-y-6" data-testid="settings-page">
      <PageHeader
        eyebrow="管理"
        title="任务与配置"
        description="后台任务的运行情况、周期与开关；改的值存进数据库并立即生效，无需重启。"
        icon={<SlidersHorizontal className="size-5" />}
      />

      <Notice tone="warn">
        覆盖优先级：<strong>数据库覆盖值 &gt; .env</strong>。直接改 .env 对已覆盖的项无效——
        请先在此「恢复默认」，或直接改本页的值。启动期项（密钥、端口、数据目录、上游白名单）
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
        ) : isError ? (
          <QueryErrorState
            title="运行时配置加载失败"
            description={errorText(settingsError, "拿不到运行时配置，请稍后重试。")}
            onRetry={() => void refetch()}
            data-testid="settings-error-state"
          />
        ) : settings.length === 0 ? (
          <EmptyState
            icon={<SlidersHorizontal className="size-5" />}
            title="没有可热更配置"
            description="当前没有可在线调整的配置。"
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
                {hasSections && (
                  <div className="mb-3 overflow-x-auto pb-1">
                    <Tabs
                      value={activeSection}
                      options={[
                        { value: "tasks", label: "后台任务" },
                        { value: "gateway", label: "网关配置" },
                      ]}
                      onChange={(value) => setSection(value as "tasks" | "gateway")}
                      testId="settings-sections"
                      compact
                    />
                  </div>
                )}

                {activeSection === "tasks" ? (
                  <>
                    <div className="mb-3 overflow-x-auto pb-1">
                      <Tabs
                        value={activeTaskGroup ?? ""}
                        options={taskGroupOptions}
                        onChange={setTaskGroupKey}
                        testId="settings-task-groups"
                        compact
                      />
                    </div>

                    {/* 组内只有一个任务时不摆第三排 tab：一排只有一个选项只是噪音 */}
                    {taskGroupTasks.length > 1 && (
                      <div className="mb-3 overflow-x-auto pb-1">
                        <Tabs
                          value={activeTask?.key ?? ""}
                          options={taskGroupTasks.map((task) => ({
                            value: task.key,
                            label: tabText(task),
                          }))}
                          onChange={setTaskKey}
                          testId="settings-task-tabs"
                          compact
                        />
                      </div>
                    )}

                    {activeTask ? (
                      <>
                        <TaskCard
                          task={activeTask}
                          serverTime={serverTime}
                          rows={renderSettings(activeRows)}
                          running={runningKey === activeTask.key}
                          result={runResults[activeTask.key] ?? null}
                          onRun={() => void triggerRun(activeTask.key)}
                        />
                        {runError && runError.key === activeTask.key && (
                          <div data-testid="task-run-error" role="alert">
                            <Notice tone="danger">{runError.text}</Notice>
                          </div>
                        )}
                      </>
                    ) : (
                      <p className="text-xs text-muted-foreground" data-testid="no-task-in-group">
                        该分组下没有已装配的任务。
                      </p>
                    )}
                  </>
                ) : (
                  <>
                    <div className="mb-3 overflow-x-auto pb-1">
                      <Tabs
                        value={activeGatewayGroup ?? ""}
                        options={gatewayGroupOptions}
                        onChange={setGatewayGroupKey}
                        testId="settings-gateway-groups"
                        compact
                      />
                    </div>
                    {activeRows.length > 0 ? (
                      renderSettings(activeRows)
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
