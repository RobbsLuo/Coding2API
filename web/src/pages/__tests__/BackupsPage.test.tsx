import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { BackupsPage, formatBytes } from "../BackupsPage";
import { Layout } from "@/Layout";
import { fullSession, jsonResponse } from "@/test-utils";

const SESSION = fullSession({ username: "root", is_admin: true });
const BACKUP = { name: "coding2api-20261028-120000.sqlite3", size: 2 * 1024 * 1024,
                 created_at: 1_700_000_000 };

/** 按「方法 + 路径」分派 fetch：备份页同一路径 GET/POST 语义不同。 */
function stubFetch(handlers: Record<string, (url: string) => Response | Promise<Response>>) {
  const spy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    const method = init?.method ?? "GET";
    const path = url.split("?")[0];
    const handler = handlers[`${method} ${path}`] ?? handlers[`${method} *`];
    // Layout 每页都会拉侧栏角标；与本页无关的请求给空壳，避免每个用例重复 mock
    if (!handler && path === "/api/nav-badges") return jsonResponse({});
    if (!handler) throw new Error(`未 mock 的请求: ${method} ${url}`);
    return handler(url);
  });
  vi.stubGlobal("fetch", spy);
  return spy;
}

function renderPage() {
  return render(
    <QueryClientProvider client={new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    })}>
      <MemoryRouter initialEntries={["/backups"]}>
        <Routes>
          <Route element={<Layout session={SESSION} />}>
            <Route path="/backups" element={<BackupsPage />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("formatBytes", () => {
  it("按量级选单位", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2.0 KB");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.0 MB");
  });
});

describe("BackupsPage", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("渲染备份列表与待恢复提示", async () => {
    stubFetch({
      "GET /api/backups": () => jsonResponse({
        backups: [BACKUP],
        pending: { filename: "old.sqlite3", size: 4096, uploaded_at: 1_700_000_000 },
      }),
    });
    renderPage();
    expect(await screen.findByTestId("backups-table")).toBeInTheDocument();
    const row = screen.getByTestId(`backup-row-${BACKUP.name}`);
    expect(within(row).getByText("2.0 MB")).toBeInTheDocument();
    expect(screen.getByTestId("pending-restore")).toHaveTextContent("old.sqlite3");
    expect(screen.getByTestId("pending-restore")).toHaveTextContent("重启服务后生效");
  });

  it("空列表给出引导语", async () => {
    stubFetch({ "GET /api/backups": () => jsonResponse({ backups: [], pending: null }) });
    renderPage();
    expect(await screen.findByText("还没有备份")).toBeInTheDocument();
  });

  it("立即备份成功后提示并刷新", async () => {
    let created = false;
    stubFetch({
      "GET /api/backups": () => jsonResponse({
        backups: created ? [BACKUP] : [],
        pending: null,
      }),
      "POST /api/backups": () => {
        created = true;
        return jsonResponse({ backup: BACKUP });
      },
    });
    renderPage();
    await userEvent.click(await screen.findByTestId("create-backup"));
    await waitFor(() => expect(screen.getByTestId("backup-notice"))
      .toHaveTextContent(`已创建备份 ${BACKUP.name}`));
    await waitFor(() => expect(screen.getByTestId("backups-table")).toBeInTheDocument());
  });

  it("删除需二次确认", async () => {
    const fetchSpy = stubFetch({
      "GET /api/backups": () => jsonResponse({ backups: [BACKUP], pending: null }),
      "DELETE /api/backups/coding2api-20261028-120000.sqlite3": () =>
        jsonResponse({ ok: true }),
    });
    renderPage();
    await userEvent.click(await screen.findByTestId(`delete-${BACKUP.name}`));
    expect(screen.getByTestId("backup-confirm")).toHaveTextContent("不可撤销");
    await userEvent.click(screen.getByTestId("backup-confirm-ok"));

    await waitFor(() => expect(screen.getByTestId("backup-notice"))
      .toHaveTextContent(`已删除 ${BACKUP.name}`));
    expect(fetchSpy).toHaveBeenCalledWith(
      `/api/backups/${BACKUP.name}`,
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("一键恢复：先下载再上传暂存", async () => {
    stubFetch({
      "GET /api/backups": () => jsonResponse({ backups: [BACKUP], pending: null }),
      [`GET /api/backups/${BACKUP.name}`]: () =>
        new Response(new Blob([new Uint8Array([1, 2, 3])]), { status: 200 }),
      "POST /api/backups/restore": () => jsonResponse({
        staged: true, restart_required: true, filename: BACKUP.name, size: 3,
        uploaded_at: 1_700_000_000,
      }),
    });
    renderPage();
    await userEvent.click(await screen.findByTestId(`restore-${BACKUP.name}`));
    expect(screen.getByTestId("backup-confirm")).toHaveTextContent("重启服务后覆盖当前数据库");
    await userEvent.click(screen.getByTestId("backup-confirm-ok"));

    await waitFor(() => expect(screen.getByTestId("backup-notice"))
      .toHaveTextContent("重启服务后生效"));
  });

  it("上传文件走确认对话框", async () => {
    const fetchSpy = stubFetch({
      "GET /api/backups": () => jsonResponse({ backups: [], pending: null }),
      "POST /api/backups/restore": () => jsonResponse({
        staged: true, restart_required: true, filename: "upload.sqlite3", size: 3,
        uploaded_at: 1_700_000_000,
      }),
    });
    renderPage();
    const file = new File([new Uint8Array([1, 2, 3])], "upload.sqlite3",
                          { type: "application/octet-stream" });
    await userEvent.upload(screen.getByTestId("backup-file"), file);
    expect(screen.getByTestId("backup-confirm")).toHaveTextContent("upload.sqlite3");
    await userEvent.click(screen.getByTestId("backup-confirm-ok"));

    await waitFor(() => expect(screen.getByTestId("backup-notice"))
      .toHaveTextContent("重启服务后生效"));
    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/backups/restore?filename=upload.sqlite3",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("列表加载失败显示错误态", async () => {
    stubFetch({
      "GET /api/backups": () => new Response("boom", { status: 500 }),
    });
    renderPage();
    expect(await screen.findByTestId("backups-load-error")).toBeInTheDocument();
  });

  it("操作失败显示后端文案", async () => {
    stubFetch({
      "GET /api/backups": () => jsonResponse({ backups: [], pending: null }),
      "POST /api/backups": () => jsonResponse(
        { error: { code: "invalid_request", message: "主库不存在" } }, 400),
    });
    renderPage();
    await userEvent.click(await screen.findByTestId("create-backup"));
    await waitFor(() => expect(screen.getByTestId("backup-error"))
      .toHaveTextContent("主库不存在"));
  });
});
