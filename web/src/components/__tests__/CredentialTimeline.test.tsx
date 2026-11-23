import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CredentialTimeline } from "../CredentialTimeline";

function mockFetch(handler: (url: string) => Response | Promise<Response>) {
  const spy = vi.fn(async (input: RequestInfo | URL) => handler(String(input)));
  vi.stubGlobal("fetch", spy);
  return spy;
}

function page(body: unknown, ok = true, status = 200): Response {
  return {
    ok,
    status,
    statusText: ok ? "OK" : "Bad Request",
    json: async () => body,
  } as unknown as Response;
}

const event = {
  id: "cevt_1",
  credential_id: "cred_1",
  ts: 1_700_000_000,
  event: "cooldown_start",
  reason: "soft",
  model: "glm-5.2",
  detail: "模型级冷却至 1700003600（第 1 次）",
  request_id: null,
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("CredentialTimeline", () => {
  it("默认只显示入口，点击才发请求（列表页不为每行预取历史）", async () => {
    const spy = mockFetch(() => page({ events: [event], limit: 20, has_more: false }));
    render(<CredentialTimeline credentialId="cred_1" />);

    expect(spy).not.toHaveBeenCalled();
    await userEvent.click(screen.getByTestId("timeline-load"));

    await waitFor(() => expect(screen.getByTestId("timeline-table")).toBeInTheDocument());
    expect(spy.mock.calls[0][0]).toContain("/api/credentials/cred_1/timeline?limit=20&offset=0");
    expect(screen.getByText("进入冷却")).toBeInTheDocument();
    expect(screen.getByText("glm-5.2")).toBeInTheDocument();
    // 未知事件类型原样显示（后端加枚举时不至于空白）
    expect(screen.queryByTestId("timeline-more")).not.toBeInTheDocument();
  });

  it("空历史给出可读提示，而不是空表", async () => {
    mockFetch(() => page({ events: [], limit: 20, has_more: false }));
    render(<CredentialTimeline credentialId="cred_2" />);

    await userEvent.click(screen.getByTestId("timeline-load"));
    await waitFor(() => expect(screen.getByTestId("timeline-empty")).toBeInTheDocument());
  });

  it("失败只影响本区块（一行可读原因，入口可重试）", async () => {
    mockFetch(() => page({ error: { code: "forbidden", message: "需要操作员权限" } },
                         false, 403));
    render(<CredentialTimeline credentialId="cred_3" />);

    await userEvent.click(screen.getByTestId("timeline-load"));
    await waitFor(() =>
      expect(screen.getByTestId("timeline-error")).toHaveTextContent("需要操作员权限"));
    // 错误后入口仍在（可重试），不渲染空表
    expect(screen.getByTestId("timeline-load")).toBeInTheDocument();
  });

  it("has_more 时按 offset 追加更早的记录", async () => {
    const older = { ...event, id: "cevt_0", event: "cooldown_end", model: null };
    const spy = mockFetch((url) => (url.includes("offset=0")
      ? page({ events: [event], limit: 1, has_more: true })
      : page({ events: [older], limit: 1, has_more: false })));
    render(<CredentialTimeline credentialId="cred_4" />);

    await userEvent.click(screen.getByTestId("timeline-load"));
    await waitFor(() => expect(screen.getByTestId("timeline-more")).toBeInTheDocument());
    await userEvent.click(screen.getByTestId("timeline-more"));

    await waitFor(() =>
      expect(screen.getByTestId("timeline-event-cevt_0")).toBeInTheDocument());
    // 追加而不是替换：第一条仍在
    expect(screen.getByTestId("timeline-event-cevt_1")).toBeInTheDocument();
    expect(spy.mock.calls[1][0]).toContain("offset=1");
    await waitFor(() => expect(screen.queryByTestId("timeline-more")).not.toBeInTheDocument());
  });
});
