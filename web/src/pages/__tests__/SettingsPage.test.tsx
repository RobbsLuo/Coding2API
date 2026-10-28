import { screen, waitFor, within } from "@testing-library/react";

import { beforeEach, describe, expect, it, vi } from "vitest";
import { SettingsPage } from "../SettingsPage";
import { jsonResponse, mockFetch, renderPage, settle, userEvent } from "@/test-utils";
import { GROUPS, SETTINGS, SERVER_TIME, TASK_GROUPS, body, mockAll, openGateway, openTask, runBody, tasksBody, withSettings } from "@/test-utils/settings";

describe("SettingsPage", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("渲染全部配置项，并标出来源与 .env 默认值", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();

    // 默认落在第一个任务（额度探测）
    expect(screen.getByTestId("setting-quota_probe_minutes")).toBeInTheDocument();
    expect(screen.getByTestId("source-quota_probe_minutes")).toHaveTextContent("来自 .env");

    await openGateway("gateway-pacer");
    expect(screen.getByTestId("setting-pacer_max_seconds")).toBeInTheDocument();
    expect(screen.getByTestId("source-pacer_max_seconds")).toHaveTextContent("DB 覆盖");
    expect(screen.getByTestId("default-pacer_max_seconds")).toHaveTextContent("20");

    await openTask("account", "activity");
    expect(screen.getByTestId("setting-activity_report_enabled")).toBeInTheDocument();
  });

  it("一级 tab 分后台任务 / 网关配置，两边内容不混排", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();

    // 默认在「后台任务」：只有任务分组与任务卡，没有配置分组
    expect(screen.getByTestId("settings-sections-tasks")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("settings-task-groups-account")).toBeInTheDocument();
    expect(screen.queryByTestId("settings-gateway-groups-gateway-pacer")).not.toBeInTheDocument();

    await openGateway("gateway-pacer");
    expect(screen.getByTestId("settings-sections-gateway")).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByTestId("task-quota_probe")).not.toBeInTheDocument();
    expect(screen.queryByTestId("settings-task-groups-account")).not.toBeInTheDocument();
  });

  it("任务按后端下发的业务分组二级切换，配置按归属进各自任务", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();

    // 额度探测：只有它自己的配置
    const probe = screen.getByTestId("task-quota_probe");
    expect(within(probe).getByTestId("setting-quota_probe_minutes")).toBeInTheDocument();
    expect(within(probe).queryByTestId("setting-pacer_max_seconds")).not.toBeInTheDocument();

    await openTask("account", "activity");
    expect(
      within(screen.getByTestId("task-activity")).getByTestId("setting-activity_report_enabled"),
    ).toBeInTheDocument();

    // 节流项不属于任何任务：只在「后台任务节流」这个配置分组出现
    await openGateway("gateway-pacer");
    expect(screen.getByTestId("setting-pacer_max_seconds")).toBeInTheDocument();
    expect(screen.queryByTestId("task-quota_probe")).not.toBeInTheDocument();
  });

  it("任务分组只列已装配任务的分组，配置分组只列有配置项的分组", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();

    // 样例任务都在 account；model / ops 分组没有已装配任务 → 不出 tab
    expect(screen.getByRole("tab", { name: "账号维护" })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "模型与渠道" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "运维与数据" })).not.toBeInTheDocument();

    await openGateway("gateway-pacer");
    // 有配置项的 pacer 分组有 tab；模型/渠道等当前无配置项，不出现
    expect(screen.getByRole("tab", { name: "后台任务节流" })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "模型与回退" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "上下文压缩" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "渠道节流" })).not.toBeInTheDocument();
  });

  it("组内只有一个任务时不渲染第三排任务 tab", async () => {
    const spy = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/api/tasks")) {
        return jsonResponse({
          tasks: [
            {
              key: "backup",
              name: "数据库备份",
              description: "周期性备份主库。",
              group: "ops",
              interval_seconds: 86400,
              enabled: true,
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
        });
      }
      return jsonResponse(body());
    });
    vi.stubGlobal("fetch", spy);

    renderPage(<SettingsPage />);
    await settle();

    expect(screen.getByTestId("settings-task-groups-ops")).toBeInTheDocument();
    expect(screen.queryByTestId("settings-task-tabs")).not.toBeInTheDocument();
    expect(screen.getByTestId("task-backup")).toBeInTheDocument();
  });

  it("tab 文案去掉任务名的括号补充、过长的用简称；卡片标题保留完整名", async () => {
    const spy = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/api/tasks")) {
        return jsonResponse({
          tasks: [
            {
              key: "model_catalog",
              name: "渠道模型列表刷新",
              description: "兜底重拉各渠道模型表。",
              group: "model",
              interval_seconds: 1800,
              enabled: true,
              running: false,
              runs: 0,
              last_started_at: null,
              last_finished_at: null,
              last_ok: null,
              last_report: null,
              last_error: null,
            },
            {
              key: "openrouter_catalog",
              name: "模型目录刷新（OpenRouter）",
              description: "拉取 OpenRouter 模型目录。",
              group: "model",
              interval_seconds: 86400,
              enabled: true,
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
        });
      }
      return jsonResponse(body());
    });
    vi.stubGlobal("fetch", spy);

    renderPage(<SettingsPage />);
    await settle();

    // 括号补充被去掉、过长任务名用简称
    expect(screen.getByRole("tab", { name: "模型目录刷新" })).toBeInTheDocument();
    expect(
      screen.queryByRole("tab", { name: "模型目录刷新（OpenRouter）" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "渠道模型刷新" })).toBeInTheDocument();
    // 卡片标题仍用完整名（默认落在第一个任务 model_catalog）
    expect(screen.getByTestId("task-model_catalog")).toHaveTextContent("渠道模型列表刷新");

    await openTask("model", "openrouter_catalog");
    expect(screen.getByTestId("task-openrouter_catalog")).toHaveTextContent(
      "模型目录刷新（OpenRouter）",
    );
  });

  it("任务卡片展示周期、上次执行相对时间与最近结果", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();

    // 默认落在第一个任务 tab（额度探测）
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
    await openTask("account", "checkin");

    const checkin = screen.getByTestId("task-checkin");
    expect(checkin).toHaveTextContent("上次失败");
    expect(screen.getByTestId("task-error-checkin")).toHaveTextContent("上游 500");
  });

  it("关闭的任务显示「已关闭」，未执行过的显示尚未执行", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();
    await openTask("account", "activity");

    const activity = screen.getByTestId("task-activity");
    expect(activity).toHaveTextContent("已关闭");
    expect(screen.getByTestId("task-last-activity")).toHaveTextContent("本进程内尚未执行");
  });

  it("无热更配置的任务给出提示而不是空白卡片", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();
    await openTask("account", "checkin");

    expect(screen.getByTestId("task-noconfig-checkin")).toHaveTextContent("无可热更配置");
  });

  it("任务卡片内按后端下发的分节标题聚合配置项", async () => {
    const alertSettings = [
      {
        key: "alert_enabled",
        env_name: "ALERT_ENABLED",
        label: "运维告警开关",
        description: "是否启用告警评估。",
        kind: "bool",
        value: true,
        default: true,
        overridden: false,
        task: "alert",
        group: null,
        section: "基础与迟滞",
      },
      {
        key: "alert_for_ticks",
        env_name: "ALERT_FOR_TICKS",
        label: "告警进入迟滞",
        description: "连续命中轮数。",
        kind: "int",
        value: 2,
        default: 2,
        overridden: false,
        task: "alert",
        group: null,
        section: "基础与迟滞",
      },
      {
        key: "alert_pool_ready_min",
        env_name: "ALERT_POOL_READY_MIN",
        label: "池耗尽阈值",
        description: "可用数下限。",
        kind: "int",
        value: 2,
        default: 2,
        overridden: false,
        task: "alert",
        group: null,
        section: "规则阈值",
      },
    ];
    const spy = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/api/tasks")) {
        return jsonResponse({
          tasks: [
            {
              key: "alert",
              name: "运维告警",
              description: "周期评估风险。",
              group: "ops",
              interval_seconds: 600,
              enabled: true,
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
        });
      }
      return jsonResponse({ settings: alertSettings, overridden: 0, groups: GROUPS });
    });
    vi.stubGlobal("fetch", spy);

    renderPage(<SettingsPage />);
    await settle();

    // 同节只出一个标题，节内保持后端下发顺序
    expect(screen.getByTestId("setting-section-alert_enabled")).toHaveTextContent("基础与迟滞");
    expect(screen.getAllByText("基础与迟滞")).toHaveLength(1);
    expect(screen.getByTestId("setting-alert_enabled")).toBeInTheDocument();
    expect(screen.getByTestId("setting-alert_for_ticks")).toBeInTheDocument();
    expect(screen.getByTestId("setting-section-alert_pool_ready_min")).toHaveTextContent("规则阈值");
  });

  it("拿不到任务运行态时列出提示，配置项仍留在页面上", async () => {
    const spy = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/api/tasks")) {
        return jsonResponse({ tasks: [], groups: [], server_time: SERVER_TIME });
      }
      return jsonResponse(body());
    });
    vi.stubGlobal("fetch", spy);

    renderPage(<SettingsPage />);
    await settle();

    expect(screen.getByTestId("no-tasks")).toBeInTheDocument();
    // 只有配置一边有内容：不渲染一级 tab，直接落在网关配置
    expect(screen.queryByTestId("settings-sections")).not.toBeInTheDocument();
    // 有内容的网关 tab 仍在
    expect(screen.getByTestId("setting-pacer_max_seconds")).toBeInTheDocument();
    // 任务归属项因任务清单不可用落到「其他」，不会被吞掉
    await openGateway("settings-other");
    expect(screen.getByTestId("setting-quota_probe_minutes")).toBeInTheDocument();
  });

  it("归属对不上的配置项落进「其他」tab，不会被吞掉", async () => {
    // task 指向一个未装配的任务、且无有效分组 → 兜底进「其他」
    withSettings([
      ...SETTINGS,
      {
        key: "orphan_setting",
        env_name: "ORPHAN_SETTING",
        label: "孤儿配置",
        description: "归属既非任务也非网关卡组。",
        kind: "int",
        value: 1,
        default: 1,
        overridden: false,
        task: "ghost_task",
        group: null,
      },
    ]);
    renderPage(<SettingsPage />);
    await settle();

    await openGateway("settings-other");
    expect(screen.getByTestId("setting-orphan_setting")).toBeInTheDocument();
    expect(screen.queryByTestId("task-quota_probe")).not.toBeInTheDocument();
  });

  it("布尔项用下拉选择，数字项用输入框", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();
    expect(screen.getByTestId("input-quota_probe_minutes").tagName).toBe("INPUT");

    await openTask("account", "activity");
    expect(screen.getByTestId("input-activity_report_enabled").tagName).toBe("SELECT");
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

  it("点「立即运行」触发后端并展示本轮结果", async () => {
    const spy = mockAll();
    renderPage(<SettingsPage />);
    await settle();

    await userEvent.click(screen.getByTestId("task-run-quota_probe"));

    await waitFor(() => {
      expect(screen.getByTestId("task-run-result-quota_probe")).toHaveTextContent("已运行一轮");
    });
    // 打到了正确的端点，且带了写请求所需的 CSRF 头
    const call = spy.mock.calls.find(([, init]) => init?.method === "POST");
    expect(String(call?.[0])).toContain("/api/tasks/quota_probe/run");
    expect((call?.[1]?.headers as Record<string, string>)["X-Requested-With"]).toBe("XMLHttpRequest");
  });

  it("已在跑的任务：按钮禁用并显示运行中，不重复触发", async () => {
    mockAll(undefined, () => jsonResponse(runBody("quota_probe", { busy: true, ran: false })));
    renderPage(<SettingsPage />);
    await settle();

    // 默认数据里 running=false，按钮可点；点后拿到 busy 返回
    await userEvent.click(screen.getByTestId("task-run-quota_probe"));
    await waitFor(() => {
      expect(screen.getByTestId("task-run-result-quota_probe")).toHaveTextContent(
        "正在运行，本次未重复触发",
      );
    });
  });

  it("被开关跳过的任务：按钮禁用且不谎报已运行", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();
    await openTask("account", "activity");

    // activity 在样例数据里 enabled=false
    expect(screen.getByTestId("task-run-activity")).toBeDisabled();
    expect(screen.getByTestId("task-run-activity")).toHaveAttribute(
      "title",
      "任务已关闭，先打开对应开关",
    );
  });

  it("后端回 ran=false（no-op）时如实说「本轮未执行」", async () => {
    mockAll(undefined, () => jsonResponse(runBody("quota_probe", { ran: false, busy: false })));
    renderPage(<SettingsPage />);
    await settle();

    await userEvent.click(screen.getByTestId("task-run-quota_probe"));

    await waitFor(() => {
      expect(screen.getByTestId("task-run-result-quota_probe")).toHaveTextContent("本轮未执行");
    });
  });

  it("本轮失败时展示错误原文", async () => {
    mockAll(undefined, () => jsonResponse(runBody("quota_probe", {
      last_ok: false,
      last_error: "上游 500",
    })));
    renderPage(<SettingsPage />);
    await settle();

    await userEvent.click(screen.getByTestId("task-run-quota_probe"));

    await waitFor(() => {
      expect(screen.getByTestId("task-run-result-quota_probe")).toHaveTextContent("上游 500");
    });
  });

  it("触发请求本身失败时给出兜底文案", async () => {
    mockAll(undefined, () => jsonResponse({ error: { code: "invalid", message: "任务不存在" } }, 400));
    renderPage(<SettingsPage />);
    await settle();

    await userEvent.click(screen.getByTestId("task-run-quota_probe"));

    await waitFor(() => {
      expect(screen.getByTestId("task-run-error")).toHaveTextContent("任务不存在");
    });
  });

  it("管理员可见导航入口，非管理员不可见", async () => {
    mockAll();
    const view = renderPage(<SettingsPage />);
    await settle();
    // 管理类入口直接展示在侧边栏「管理」分组下（不再收进下拉）
    const nav = screen.getByRole("navigation", { name: "主导航" });
    expect(within(nav).getByText("任务与配置")).toBeInTheDocument();

    view.unmount();
    renderPage(<SettingsPage />, { username: "guest", is_admin: false });
    // 非管理员不渲染管理分组
    await waitFor(() => {
      const nav2 = screen.getByRole("navigation", { name: "主导航" });
      expect(within(nav2).queryByText("任务与配置")).not.toBeInTheDocument();
    });
  });
});
