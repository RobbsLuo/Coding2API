import { screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { LogsPage, filterLines, formatLogTime } from "../LogsPage";
import { MockEventSource, mockFetch, jsonResponse, renderPage, settle, userEvent } from "@/test-utils";
import type { LogLine } from "@/api/logs";

function line(overrides: Partial<LogLine> = {}): LogLine {
  return {
    seq: 1,
    ts: 1_700_000_000.5,
    level: "INFO",
    logger: "src.api.chat",
    message: "请求完成",
    ...overrides,
  };
}

const SNAPSHOT = {
  capacity: 500,
  limit: 500,
  lines: [
    line({ seq: 1, message: "启动预热完成" }),
    line({ seq: 2, level: "WARNING", logger: "src.provider.trae", message: "上游 500" }),
  ],
};

describe("LogsPage 纯函数", () => {
  it("按级别与关键字过滤（消息或 logger 命中）", () => {
    const lines = [
      line({ seq: 1, level: "INFO", message: "普通行", logger: "src.a" }),
      line({ seq: 2, level: "ERROR", message: "炸了", logger: "src.b" }),
    ];
    expect(filterLines(lines, new Set(["INFO"]), "").map((item) => item.seq)).toEqual([1]);
    expect(filterLines(lines, new Set(["INFO", "ERROR"]), "炸").map((item) => item.seq))
      .toEqual([2]);
    // 关键字也匹配 logger 名；大小写不敏感、两端空白忽略
    expect(filterLines(lines, new Set(["INFO", "ERROR"]), "  SRC.A ").map((item) => item.seq))
      .toEqual([1]);
  });

  it("按 request_id 过滤整条请求链路（支持页面上的 # 前缀）", () => {
    const lines = [
      line({ seq: 1, message: "换号 cred_a", request_id: "ab12cd34ef" }),
      line({ seq: 2, message: "换号 cred_b", request_id: "ab12cd34ef" }),
      line({ seq: 3, message: "另一条请求", request_id: "ff00ff00ff" }),
      line({ seq: 4, message: "后台任务（无请求上下文）" }),
    ];
    expect(filterLines(lines, new Set(["INFO"]), "ab12cd34").map((item) => item.seq))
      .toEqual([1, 2]);
    expect(filterLines(lines, new Set(["INFO"]), "#AB12CD34").map((item) => item.seq))
      .toEqual([1, 2]);
  });

  it("时间戳格式化到毫秒，非法值给破折号", () => {
    expect(formatLogTime(0)).toMatch(/^\d{2}:\d{2}:\d{2}\.\d{3}$/);
    expect(formatLogTime(Number.NaN)).toBe("—");
  });
});

describe("LogsPage", () => {
  beforeEach(() => {
    MockEventSource.reset();
  });

  it("首次加载拉快照并建 SSE，实时行追加", async () => {
    mockFetch({ "/api/logs": SNAPSHOT });
    renderPage(<LogsPage />, { username: "root", is_admin: true });
    await settle();

    expect(screen.getByTestId("logs-list")).toHaveTextContent("启动预热完成");
    expect(screen.getAllByTestId("log-row")).toHaveLength(2);

    const source = MockEventSource.last();
    expect(source.url).toContain("/api/logs/stream");
    source.emit(line({ seq: 3, message: "新的一条" }));
    await waitFor(() => {
      expect(screen.getAllByTestId("log-row")).toHaveLength(3);
    });
    // 按 seq 顺序渲染（旧在上）
    expect(screen.getAllByTestId("log-row")[2]).toHaveTextContent("新的一条");
  });

  it("重复 seq 去重，坏帧丢弃但连接继续", async () => {
    mockFetch({ "/api/logs": SNAPSHOT });
    renderPage(<LogsPage />, { username: "root", is_admin: true });
    await settle();
    const source = MockEventSource.last();
    source.emit(SNAPSHOT.lines[0]);          // 与快照重复
    source.emitRaw("{ half json");
    source.emit({ message: "没有 seq" });     // 形状不对
    source.emit(line({ seq: 4, message: "有效行" }));
    await waitFor(() => {
      expect(screen.getAllByTestId("log-row")).toHaveLength(3);
    });
    expect(screen.getByTestId("logs-list")).toHaveTextContent("有效行");
  });

  it("级别筛选与关键字过滤都在客户端完成（不再请求接口）", async () => {
    const spy = mockFetch({ "/api/logs": SNAPSHOT });
    renderPage(<LogsPage />, { username: "root", is_admin: true });
    await settle();
    const callsBefore = spy.mock.calls.length;

    await userEvent.click(screen.getByTestId("logs-level-INFO"));
    expect(screen.getAllByTestId("log-row")).toHaveLength(1);
    expect(screen.getByTestId("logs-list")).toHaveTextContent("上游 500");

    await userEvent.type(screen.getByTestId("logs-keyword"), "不存在");
    expect(screen.getByTestId("logs-empty")).toHaveTextContent("没有符合筛选条件的行");

    // 筛选只改本地状态：除首次快照外没有新请求
    expect(spy.mock.calls.length).toBe(callsBefore);
  });

  it("暂停断开连接，继续后重连并回读快照", async () => {
    mockFetch({ "/api/logs": SNAPSHOT });
    renderPage(<LogsPage />, { username: "root", is_admin: true });
    await settle();
    const first = MockEventSource.last();
    expect(screen.getByTestId("logs-status")).toHaveTextContent("实时");

    await userEvent.click(screen.getByTestId("logs-pause"));
    expect(first.closed).toBe(true);
    expect(screen.getByTestId("logs-status")).toHaveTextContent("已暂停");

    await userEvent.click(screen.getByTestId("logs-pause"));
    await waitFor(() => {
      expect(MockEventSource.instances.length).toBeGreaterThan(1);
    });
    expect(screen.getByTestId("logs-status")).toHaveTextContent("实时");
  });

  it("清空后显示空态，新行照常追加", async () => {
    mockFetch({ "/api/logs": SNAPSHOT });
    renderPage(<LogsPage />, { username: "root", is_admin: true });
    await settle();

    await userEvent.click(screen.getByTestId("logs-clear"));
    expect(screen.getByTestId("logs-empty")).toHaveTextContent("还没有日志");

    MockEventSource.last().emit(line({ seq: 9, message: "清空后的新行" }));
    await waitFor(() => {
      expect(screen.getByTestId("logs-list")).toHaveTextContent("清空后的新行");
    });
  });

  it("断线（浏览器放弃重连）时给出明确提示", async () => {
    mockFetch({ "/api/logs": SNAPSHOT });
    renderPage(<LogsPage />, { username: "root", is_admin: true });
    await settle();
    MockEventSource.last().fail(MockEventSource.CLOSED);
    await waitFor(() => {
      expect(screen.getByTestId("logs-status")).toHaveTextContent("实时连接已断开");
    });
  });

  it("后端关闭（404）时提示开关位置，而不是报错", async () => {
    mockFetch({
      "/api/logs": () => jsonResponse(
        { error: { code: "not_found", message: "log page disabled" } }, 404),
    });
    renderPage(<LogsPage />, { username: "root", is_admin: true });
    await waitFor(() => {
      expect(screen.getByTestId("logs-disabled")).toHaveTextContent("log_buffer_lines");
    });
    expect(screen.queryByTestId("logs-error")).not.toBeInTheDocument();
    expect(MockEventSource.instances).toHaveLength(0);
  });

  it("其它读取失败显示错误提示", async () => {
    mockFetch({
      "/api/logs": () => jsonResponse({ error: { code: "boom", message: "炸" } }, 500),
    });
    renderPage(<LogsPage />, { username: "root", is_admin: true });
    await waitFor(() => {
      expect(screen.getByTestId("logs-error")).toBeInTheDocument();
    });
  });

  it("request_id 以短前缀展示（完整值在 title 里）", async () => {
    mockFetch({
      "/api/logs": {
        capacity: 10, limit: 10,
        lines: [line({ seq: 1, request_id: "abcdef0123456789" })],
      },
    });
    renderPage(<LogsPage />, { username: "root", is_admin: true });
    await settle();
    const chip = screen.getByTestId("log-request-id");
    expect(chip).toHaveTextContent("#abcdef01");
    expect(chip).toHaveAttribute(
      "title", "只看这条请求的全部日志（request_id=abcdef0123456789）");
  });

  it("点 request_id 徽标即筛出该请求的整条链路", async () => {
    mockFetch({
      "/api/logs": {
        capacity: 10, limit: 10,
        lines: [
          line({ seq: 1, message: "换号 cred_a", request_id: "abcdef0123456789" }),
          line({ seq: 2, message: "另一条请求", request_id: "1234567890abcdef" }),
        ],
      },
    });
    renderPage(<LogsPage />, { username: "root", is_admin: true });
    await settle();
    expect(screen.getAllByTestId("log-row")).toHaveLength(2);

    await userEvent.click(screen.getAllByTestId("log-request-id")[0]);
    expect(screen.getByTestId("logs-keyword")).toHaveValue("abcdef0123456789");
    await waitFor(() => {
      expect(screen.getAllByTestId("log-row")).toHaveLength(1);
    });
  });

  it("跟随最新可关闭（不改变已渲染内容）", async () => {
    mockFetch({ "/api/logs": SNAPSHOT });
    renderPage(<LogsPage />, { username: "root", is_admin: true });
    await settle();
    const follow = screen.getByTestId("logs-follow");
    await userEvent.click(follow);
    expect(follow).not.toBeChecked();
    expect(screen.getAllByTestId("log-row")).toHaveLength(2);
  });
});
