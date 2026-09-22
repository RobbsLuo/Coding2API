import { screen, waitFor, within } from "@testing-library/react";
import { useQuery } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SettingsPage } from "../SettingsPage";
import { jsonResponse, mockFetch, renderPage, settle, userEvent } from "./helpers";

const SETTINGS = [
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
  },
];

const SERVER_TIME = 1_700_000_000;

const TASKS = {
  tasks: [
    {
      key: "quota_probe",
      name: "额度探测",
      description: "探测上游剩余额度并写回凭证健康度。",
      interval_seconds: 3600,
      enabled: true,
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
      interval_seconds: 600,
      enabled: true,
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
      interval_seconds: 600,
      enabled: false,
      runs: 0,
      last_started_at: null,
      last_finished_at: null,
      last_ok: null,
      last_report: null,
      last_error: null,
    },
  ],
  server_time: SERVER_TIME,
};

/** GET 返回体：mockFetch 每次按值 new Response，避免 body 被消费一次后拿不到。 */
const body = () => JSON.parse(JSON.stringify({ settings: SETTINGS, overridden: 1 }));
const tasksBody = () => JSON.parse(JSON.stringify(TASKS));

/** 统一 fetch mock：GET 分派 settings/tasks，PUT 交给调用方断言。 */
function mockAll(put?: (init: RequestInit) => Response) {
  const spy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    if (init?.method === "PUT") return put ? put(init) : jsonResponse(body());
    if (url.includes("/api/tasks")) return jsonResponse(tasksBody());
    return jsonResponse(body());
  });
  vi.stubGlobal("fetch", spy);
  return spy;
}

function withSettings(settings: unknown[]) {
  const spy = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url.includes("/api/tasks")) return jsonResponse(tasksBody());
    return jsonResponse({ settings, overridden: 0 });
  });
  vi.stubGlobal("fetch", spy);
  return spy;
}

describe("SettingsPage", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("渲染全部配置项，并标出来源与 .env 默认值", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();

    expect(screen.getByTestId("setting-quota_probe_minutes")).toBeInTheDocument();
    expect(screen.getByTestId("setting-pacer_max_seconds")).toBeInTheDocument();
    expect(screen.getByTestId("setting-activity_report_enabled")).toBeInTheDocument();

    expect(screen.getByTestId("source-pacer_max_seconds")).toHaveTextContent("DB 覆盖");
    expect(screen.getByTestId("source-quota_probe_minutes")).toHaveTextContent("来自 .env");
    expect(screen.getByTestId("default-pacer_max_seconds")).toHaveTextContent("20");
  });

  it("配置按后端下发的归属进任务卡片，无归属的进网关区", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();

    const probe = screen.getByTestId("task-quota_probe");
    expect(within(probe).getByTestId("setting-quota_probe_minutes")).toBeInTheDocument();
    expect(
      within(screen.getByTestId("task-activity")).getByTestId("setting-activity_report_enabled"),
    ).toBeInTheDocument();
    // 节流项不属于任何任务：不能出现在任务卡片里
    expect(within(probe).queryByTestId("setting-pacer_max_seconds")).not.toBeInTheDocument();
    expect(screen.getByTestId("setting-pacer_max_seconds")).toBeInTheDocument();
  });

  it("任务卡片展示周期、上次执行相对时间与最近结果", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();

    expect(screen.getByTestId("task-interval-quota_probe")).toHaveTextContent("1.0 小时");
    expect(screen.getByTestId("task-last-quota_probe")).toHaveTextContent("2 分钟前");
    expect(screen.getByTestId("task-report-quota_probe")).toHaveTextContent(
      "尝试 2 · 成功 2 · 失败 0 · 跳过 0",
    );
  });

  it("失败任务标出「上次失败」并展示错误原文", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();

    const checkin = screen.getByTestId("task-checkin");
    expect(checkin).toHaveTextContent("上次失败");
    expect(screen.getByTestId("task-error-checkin")).toHaveTextContent("上游 500");
  });

  it("关闭的任务显示「已关闭」，未执行过的显示尚未执行", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();

    const activity = screen.getByTestId("task-activity");
    expect(activity).toHaveTextContent("已关闭");
    expect(screen.getByTestId("task-last-activity")).toHaveTextContent("本进程内尚未执行");
  });

  it("无热更配置的任务给出提示而不是空白卡片", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();

    expect(screen.getByTestId("task-noconfig-checkin")).toHaveTextContent("无可热更配置");
  });

  it("拿不到任务运行态时列出提示，配置项仍留在页面上", async () => {
    const spy = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/api/tasks")) return jsonResponse({ tasks: [], server_time: SERVER_TIME });
      return jsonResponse(body());
    });
    vi.stubGlobal("fetch", spy);

    renderPage(<SettingsPage />);
    await settle();

    expect(screen.getByTestId("no-tasks")).toBeInTheDocument();
    expect(screen.getByTestId("setting-quota_probe_minutes")).toBeInTheDocument();
  });

  it("全部配置都有任务归属时不渲染网关区清单", async () => {
    withSettings(SETTINGS.filter((setting) => setting.task !== null));
    renderPage(<SettingsPage />);
    await settle();

    expect(screen.queryByTestId("setting-pacer_max_seconds")).not.toBeInTheDocument();
    expect(screen.getByTestId("no-gateway-settings")).toBeInTheDocument();
  });

  it("布尔项用下拉选择，数字项用输入框", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();

    expect(screen.getByTestId("input-activity_report_enabled").tagName).toBe("SELECT");
    expect(screen.getByTestId("input-quota_probe_minutes").tagName).toBe("INPUT");
  });

  it("没有改动时保存按钮禁用", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();

    expect(screen.getByTestId("dirty-count")).toHaveTextContent("无改动");
    expect(screen.getByTestId("save-settings")).toBeDisabled();
  });

  it("改动后保存，提交标量值并提示立即生效", async () => {
    const fetchSpy = mockAll((init) => {
      const sent = JSON.parse(String(init.body)) as { values: Record<string, unknown> };
      // 前端必须发标量（int/bool），而不是字符串——后端按类型解析
      expect(sent.values).toEqual({ quota_probe_minutes: 15, activity_report_enabled: true });
      return jsonResponse(body());
    });

    renderPage(<SettingsPage />);
    await settle();

    const input = screen.getByTestId("input-quota_probe_minutes");
    await userEvent.clear(input);
    await userEvent.type(input, "15");
    await userEvent.selectOptions(screen.getByTestId("input-activity_report_enabled"), "true");

    expect(screen.getByTestId("dirty-count")).toHaveTextContent("2 项待保存");
    await userEvent.click(screen.getByTestId("save-settings"));

    expect(await screen.findByTestId("settings-notice")).toHaveTextContent("立即生效");
    await waitFor(() =>
      expect(fetchSpy).toHaveBeenCalledWith(
        "/api/settings",
        expect.objectContaining({ method: "PUT" }),
      ),
    );
  });

  it("恢复默认提交 null，并在文案里说明回落到 .env", async () => {
    mockAll((init) => {
      expect(JSON.parse(String(init.body))).toEqual({ values: { pacer_max_seconds: null } });
      return jsonResponse(body());
    });

    renderPage(<SettingsPage />);
    await settle();
    await userEvent.click(screen.getByTestId("reset-pacer_max_seconds"));

    expect(await screen.findByTestId("settings-notice")).toHaveTextContent("恢复 .env 默认值");
  });

  it("未覆盖的项不能「恢复默认」", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();

    expect(screen.getByTestId("reset-quota_probe_minutes")).toBeDisabled();
    expect(screen.getByTestId("reset-pacer_max_seconds")).toBeEnabled();
  });

  it("非法输入（非数字）阻止保存并给出提示", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();

    const input = screen.getByTestId("input-quota_probe_minutes");
    await userEvent.clear(input);
    await userEvent.type(input, "abc");

    expect(screen.getByTestId("settings-invalid")).toHaveTextContent("填写不合法");
    expect(screen.getByTestId("save-settings")).toBeDisabled();
  });

  it("后端拒绝时展示错误文案", async () => {
    mockAll(() =>
      jsonResponse(
        {
          error: {
            code: "invalid_request",
            message: "pacer_min_seconds 不能大于 pacer_max_seconds",
          },
        },
        400,
      ),
    );

    renderPage(<SettingsPage />);
    await settle();
    const input = screen.getByTestId("input-quota_probe_minutes");
    await userEvent.clear(input);
    await userEvent.type(input, "30");
    await userEvent.click(screen.getByTestId("save-settings"));

    expect(await screen.findByTestId("settings-error")).toHaveTextContent("不能大于");
  });

  it("保存设置后失效 Playground 模型缓存（黑名单/默认模型改动立即反映）", async () => {
    const modelsCalls = { count: 0 };
    function ModelsProbe() {
      // 模拟 PlaygroundPage 的列表查询（同一 queryKey）
      useQuery({
        queryKey: ["playground-models"],
        queryFn: async () => {
          modelsCalls.count += 1;
          return { object: "list", data: [] };
        },
      });
      return null;
    }

    mockAll();
    renderPage(
      <>
        <ModelsProbe />
        <SettingsPage />
      </>,
    );
    await settle();
    await waitFor(() => expect(modelsCalls.count).toBe(1));

    const input = screen.getByTestId("input-quota_probe_minutes");
    await userEvent.clear(input);
    await userEvent.type(input, "15");
    await userEvent.click(screen.getByTestId("save-settings"));

    await waitFor(() => expect(modelsCalls.count).toBe(2));
  });

  it("空列表给出提示", async () => {
    mockFetch({
      "/api/settings": () => jsonResponse({ settings: [], overridden: 0 }),
      "/api/tasks": () => jsonResponse(tasksBody()),
    });
    renderPage(<SettingsPage />);
    await settle();
    expect(screen.getByTestId("no-settings")).toBeInTheDocument();
  });

  it("管理员可见导航入口，非管理员不可见", async () => {
    mockAll();
    const view = renderPage(<SettingsPage />);
    await settle();
    expect(await screen.findByRole("link", { name: /任务与配置/ })).toBeInTheDocument();

    view.unmount();
    renderPage(<SettingsPage />, { username: "guest", is_admin: false });
    await waitFor(() => {
      expect(screen.queryByRole("link", { name: /任务与配置/ })).not.toBeInTheDocument();
    });
  });
});
