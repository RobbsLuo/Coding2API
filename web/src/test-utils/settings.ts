/** SettingsPage 测试共享的样例数据与 fetch mock。 */
import { screen } from "@testing-library/react";
import { vi } from "vitest";
import { jsonResponse, userEvent } from "@/test-utils";

export const SETTINGS = [
  {
    key: "quota_probe_minutes",
    env_name: "QUOTA_PROBE_MINUTES",
    label: "额度探测周期（分钟）",
    description: "后台额度探测的一轮间隔；下限 1 分钟。",
    kind: "int",
    value: 60,
    default: 60,
    overridden: false,
    task: "quota_probe",
    group: null,
  },
  {
    key: "pacer_max_seconds",
    env_name: "PACER_MAX_SECONDS",
    label: "后台任务节流上限（秒）",
    description: "必须不小于下限。",
    kind: "float",
    value: 42.5,
    default: 20,
    overridden: true,
    task: null,
    group: "pacer",
  },
  {
    key: "activity_report_enabled",
    env_name: "ACTIVITY_REPORT_ENABLED",
    label: "活跃上报",
    description: "为账号补发对话事件。",
    kind: "bool",
    value: false,
    default: false,
    overridden: false,
    task: "activity",
    group: null,
  },
];

/** 后端下发的网关卡组（顺序即展示顺序）。 */
export const GROUPS = [
  { value: "model_routing", label: "模型与回退" },
  { value: "compress", label: "上下文压缩" },
  { value: "stream_thinking", label: "流式与思考" },
  { value: "gateway_control", label: "网关与计费" },
  { value: "affinity", label: "选号与会话" },
  { value: "chat_throttle", label: "聊天节流" },
  { value: "codearts_throttle", label: "CodeArts 节流" },
  { value: "pacer", label: "后台任务节流" },
  { value: "observability", label: "可观测性" },
];

/** 后端下发的任务业务分组（顺序即展示顺序）。 */
export const TASK_GROUPS = [
  { value: "account", label: "账号维护" },
  { value: "model", label: "模型与渠道" },
  { value: "ops", label: "运维与数据" },
];

export const SERVER_TIME = 1_700_000_000;

export const TASKS = {
  tasks: [
    {
      key: "quota_probe",
      name: "额度探测",
      description: "探测上游剩余额度并写回凭证健康度。",
      group: "account",
      interval_seconds: 3600,
      enabled: true,
      running: false,
      runs: 5,
      last_started_at: SERVER_TIME - 130,
      last_finished_at: SERVER_TIME - 120,
      last_ok: true,
      last_report: { attempted: 2, succeeded: 2, failed: 0, skipped: 0 },
      last_error: null,
    },
    {
      key: "checkin",
      name: "每日签到",
      description: "全天每 10 分钟检查一次。",
      group: "account",
      interval_seconds: 600,
      enabled: true,
      running: false,
      runs: 3,
      last_started_at: SERVER_TIME - 310,
      last_finished_at: SERVER_TIME - 300,
      last_ok: false,
      last_report: null,
      last_error: "上游 500",
    },
    {
      key: "activity",
      name: "活跃上报",
      description: "按配置时点补发对话事件。",
      group: "account",
      interval_seconds: 600,
      enabled: false,
      running: false,
      runs: 0,
      last_started_at: null,
      last_finished_at: null,
      last_ok: null,
      last_report: null,
      last_error: null,
    },
  ],
  groups: TASK_GROUPS,
  server_time: SERVER_TIME,
};

/** GET 返回体：mockFetch 每次按值 new Response，避免 body 被消费一次后拿不到。 */
export const body = () =>
  JSON.parse(JSON.stringify({ settings: SETTINGS, overridden: 1, groups: GROUPS }));
export const tasksBody = () => JSON.parse(JSON.stringify(TASKS));

/** 统一 fetch mock：GET 分派 settings/tasks，PUT 交给调用方断言。 */
export function mockAll(put?: (init: RequestInit) => Response,
                        run?: (url: string) => Response) {
  const spy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    if (init?.method === "POST" && url.includes("/run")) {
      return run ? run(url) : jsonResponse(runBody("quota_probe"));
    }
    if (init?.method === "PUT") return put ? put(init) : jsonResponse(body());
    if (url.includes("/api/tasks")) return jsonResponse(tasksBody());
    return jsonResponse(body());
  });
  vi.stubGlobal("fetch", spy);
  return spy;
}

/** POST /api/tasks/{key}/run 的成功返回：任务最新运行态 + 本轮结果。 */
export function runBody(
  key: string,
  overrides: Record<string, unknown> = {},
) {
  const base = (TASKS.tasks.find((task) => task.key === key) ?? TASKS.tasks[0]) as
    Record<string, unknown>;
  return JSON.parse(JSON.stringify({
    ...base,
    runs: (base.runs as number) + 1,
    last_finished_at: SERVER_TIME,
    last_ok: true,
    last_error: null,
    last_report: { attempted: 2, succeeded: 2, failed: 0, skipped: 0 },
    ran: true,
    busy: false,
    ...overrides,
  }));
}

export function withSettings(settings: unknown[]) {
  const spy = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url.includes("/api/tasks")) return jsonResponse(tasksBody());
    return jsonResponse({ settings, overridden: 0, groups: GROUPS });
  });
  vi.stubGlobal("fetch", spy);
  return spy;
}

/** 切到某个任务：一级「后台任务」→ 业务分组 → 任务（单任务分组无第三排）。 */
export async function openTask(group: string, key: string) {
  const section = screen.queryByTestId("settings-sections-tasks");
  if (section) await userEvent.click(section);
  const groupTab = screen.queryByTestId(`settings-task-groups-${group}`);
  if (groupTab) await userEvent.click(groupTab);
  const taskTab = screen.queryByTestId(`settings-task-tabs-${key}`);
  if (taskTab) await userEvent.click(taskTab);
}

/** 切到某个网关配置分组（其他 → settings-other）。 */
export async function openGateway(value: string) {
  const section = screen.queryByTestId("settings-sections-gateway");
  if (section) await userEvent.click(section);
  await userEvent.click(screen.getByTestId(`settings-gateway-groups-${value}`));
}
