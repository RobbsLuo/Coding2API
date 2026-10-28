import { screen, waitFor } from "@testing-library/react";
import { useQuery } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SettingsPage } from "../SettingsPage";
import { jsonResponse, renderPage, settle, userEvent } from "@/test-utils";
import { body, mockAll, openGateway, openTask } from "@/test-utils/settings";

describe("SettingsPage 保存与热更", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
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

    // 换到另一个任务的 tab 再改，草稿跨 tab 保留，保存按钮统一汇总
    await openTask("account", "activity");
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
    await openGateway("gateway-pacer");
    await userEvent.click(screen.getByTestId("reset-pacer_max_seconds"));

    expect(await screen.findByTestId("settings-notice")).toHaveTextContent("恢复 .env 默认值");
  });

  it("未覆盖的项不能「恢复默认」", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();

    expect(screen.getByTestId("reset-quota_probe_minutes")).toBeDisabled();

    await openGateway("gateway-pacer");
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
});
