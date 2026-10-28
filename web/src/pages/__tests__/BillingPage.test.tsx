import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { BillingPage } from "../BillingPage";
import { jsonResponse, renderPage } from "@/test-utils";

const REPORT = {
  month: "2026-10",
  start: 1,
  end: 2,
  users: [
    { username: "alice", requests: 12, ok_count: 11, input_tokens: 12000,
      output_tokens: 3000, cached_tokens: null, credit: null, cost_usd: 0.5,
      cost_cny: 3.6 },
  ],
  keys: [
    { api_key_id: "key_123456789", key_name: "ci", username: "alice",
      requests: 12, ok_count: 11, input_tokens: 12000, output_tokens: 3000,
      cached_tokens: null, credit: null, cost_usd: 0.5, cost_cny: 3.6 },
  ],
  totals: {
    requests: 12, input_tokens: 12000, output_tokens: 3000,
    cost_cny: 3.6, cost_usd: 0.5, success_rate: 11 / 12,
  },
};

function mockFetch(payload: unknown = REPORT) {
  // 收下 URL：断言「切月份后重新拉取」要读 calls 里的请求地址
  const fetchMock = vi.fn(async (_url: string) => jsonResponse(payload));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("BillingPage", () => {
  it("渲染汇总与两张表", async () => {
    mockFetch();
    renderPage(<BillingPage />);
    expect(await screen.findByTestId("billing-users")).toHaveTextContent("alice");
    expect(screen.getByTestId("billing-keys")).toHaveTextContent("ci");
    expect(screen.getByTestId("billing-totals")).toHaveTextContent("¥3.6");
    // 汇总里的成功率按百分比展示
    expect(screen.getByTestId("billing-totals")).toHaveTextContent("91.7%");
  });

  it("空数据时给出保留期说明", async () => {
    mockFetch({ ...REPORT, users: [], keys: [] });
    renderPage(<BillingPage />);
    expect(await screen.findByText("该月没有请求记录")).toBeInTheDocument();
    expect(screen.getByText(/明细只保留 90 天/)).toBeInTheDocument();
  });

  it("接口失败展示错误提示", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(
      { error: { code: "forbidden", message: "需要操作员权限" } }, 403)));
    renderPage(<BillingPage />);
    expect(await screen.findByText("需要操作员权限")).toBeInTheDocument();
  });

  it("切换月份重新拉取账单", async () => {
    const fetchMock = mockFetch();
    renderPage(<BillingPage />);
    await screen.findByTestId("billing-users");
    fireEvent.change(screen.getByTestId("billing-month"),
                     { target: { value: "2026-08" } });
    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([url]) =>
        String(url).includes("month=2026-08"))).toBe(true);
    });
  });

  it("导出 CSV 走 blob 下载并提示", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (String(url).includes("format=csv")) {
        return new Response("username,requests\n", { status: 200 });
      }
      return jsonResponse(REPORT);
    });
    vi.stubGlobal("fetch", fetchMock);
    const blobs: string[] = [];
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: () => { blobs.push("blob"); return "blob:x"; },
      revokeObjectURL: () => undefined,
    });
    renderPage(<BillingPage />);
    await screen.findByTestId("billing-users");
    await userEvent.click(screen.getByTestId("billing-export-csv"));
    await waitFor(() => {
      expect(blobs).toHaveLength(1);
    });
    expect(await screen.findByText(/已导出 .* CSV 账单/)).toBeInTheDocument();
  });
});
