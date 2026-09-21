import { screen, waitFor } from "@testing-library/react";
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
  },
];

/** GET 返回体：mockFetch 每次按值 new Response，避免 body 被消费一次后拿不到。 */
const body = () => JSON.parse(JSON.stringify({ settings: SETTINGS, overridden: 1 }));

describe("SettingsPage", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("渲染全部配置项，并标出来源与 .env 默认值", async () => {
    mockFetch({ "/api/settings": () => jsonResponse(body()) });
    renderPage(<SettingsPage />);
    await settle();

    expect(screen.getByTestId("setting-quota_probe_minutes")).toBeInTheDocument();
    expect(screen.getByTestId("setting-pacer_max_seconds")).toBeInTheDocument();
    expect(screen.getByTestId("setting-activity_report_enabled")).toBeInTheDocument();

    expect(screen.getByTestId("source-pacer_max_seconds")).toHaveTextContent("DB 覆盖");
    expect(screen.getByTestId("source-quota_probe_minutes")).toHaveTextContent("来自 .env");
    expect(screen.getByTestId("default-pacer_max_seconds")).toHaveTextContent("20");
  });

  it("布尔项用下拉选择，数字项用输入框", async () => {
    mockFetch({ "/api/settings": () => jsonResponse(body()) });
    renderPage(<SettingsPage />);
    await settle();

    expect(screen.getByTestId("input-activity_report_enabled").tagName).toBe("SELECT");
    expect(screen.getByTestId("input-quota_probe_minutes").tagName).toBe("INPUT");
  });

  it("没有改动时保存按钮禁用", async () => {
    mockFetch({ "/api/settings": () => jsonResponse(body()) });
    renderPage(<SettingsPage />);
    await settle();

    expect(screen.getByTestId("dirty-count")).toHaveTextContent("无改动");
    expect(screen.getByTestId("save-settings")).toBeDisabled();
  });

  it("改动后保存，提交标量值并提示立即生效", async () => {
    const fetchSpy = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "PUT") {
        const sent = JSON.parse(String(init.body)) as { values: Record<string, unknown> };
        // 前端必须发标量（int/bool），而不是字符串——后端按类型解析
        expect(sent.values).toEqual({ quota_probe_minutes: 15, activity_report_enabled: true });
      }
      return jsonResponse(body());
    });
    vi.stubGlobal("fetch", fetchSpy);

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
    const fetchSpy = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "PUT") {
        expect(JSON.parse(String(init.body))).toEqual({ values: { pacer_max_seconds: null } });
      }
      return jsonResponse(body());
    });
    vi.stubGlobal("fetch", fetchSpy);

    renderPage(<SettingsPage />);
    await settle();
    await userEvent.click(screen.getByTestId("reset-pacer_max_seconds"));

    expect(await screen.findByTestId("settings-notice")).toHaveTextContent("恢复 .env 默认值");
  });

  it("未覆盖的项不能「恢复默认」", async () => {
    mockFetch({ "/api/settings": () => jsonResponse(body()) });
    renderPage(<SettingsPage />);
    await settle();

    expect(screen.getByTestId("reset-quota_probe_minutes")).toBeDisabled();
    expect(screen.getByTestId("reset-pacer_max_seconds")).toBeEnabled();
  });

  it("非法输入（非数字）阻止保存并给出提示", async () => {
    mockFetch({ "/api/settings": () => jsonResponse(body()) });
    renderPage(<SettingsPage />);
    await settle();

    const input = screen.getByTestId("input-quota_probe_minutes");
    await userEvent.clear(input);
    await userEvent.type(input, "abc");

    expect(screen.getByTestId("settings-invalid")).toHaveTextContent("填写不合法");
    expect(screen.getByTestId("save-settings")).toBeDisabled();
  });

  it("后端拒绝时展示错误文案", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === "PUT") {
          return jsonResponse(
            {
              error: {
                code: "invalid_request",
                message: "pacer_min_seconds 不能大于 pacer_max_seconds",
              },
            },
            400,
          );
        }
        return jsonResponse(body());
      }),
    );

    renderPage(<SettingsPage />);
    await settle();
    const input = screen.getByTestId("input-quota_probe_minutes");
    await userEvent.clear(input);
    await userEvent.type(input, "30");
    await userEvent.click(screen.getByTestId("save-settings"));

    expect(await screen.findByTestId("settings-error")).toHaveTextContent("不能大于");
  });

  it("空列表给出提示", async () => {
    mockFetch({ "/api/settings": () => jsonResponse({ settings: [], overridden: 0 }) });
    renderPage(<SettingsPage />);
    await settle();
    expect(screen.getByTestId("no-settings")).toBeInTheDocument();
  });

  it("管理员可见导航入口，非管理员不可见", async () => {
    mockFetch({ "/api/settings": () => jsonResponse(body()) });
    const view = renderPage(<SettingsPage />);
    await settle();
    expect(await screen.findByRole("link", { name: /运行时配置/ })).toBeInTheDocument();

    view.unmount();
    renderPage(<SettingsPage />, { username: "guest", is_admin: false });
    await waitFor(() => {
      expect(screen.queryByRole("link", { name: /运行时配置/ })).not.toBeInTheDocument();
    });
  });
});
