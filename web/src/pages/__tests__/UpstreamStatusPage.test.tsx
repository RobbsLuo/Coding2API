import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { UpstreamStatusPage } from "../UpstreamStatusPage";
import { renderPage, settle } from "@/test-utils";
import type { UpstreamSource, UpstreamStatusResponse } from "@/api/types";

function stubFetch(body: unknown, status = 200) {
  const spy = vi.fn(async () =>
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", spy);
  return spy;
}

function source(overrides: Partial<UpstreamSource> = {}): UpstreamSource {
  return {
    key: "kilo",
    label: "Kilo",
    scope: "channel",
    page_url: "https://status.kilo.ai",
    related_providers: ["kilo"],
    level: "operational",
    fetched_at: 1_700_000_000,
    error: null,
    source_stale: false,
    components: [],
    incidents: [],
    ...overrides,
  };
}

function payload(overrides: Partial<UpstreamStatusResponse> = {}): UpstreamStatusResponse {
  return {
    summary: { level: "operational", operational: 1, abnormal: 0, unknown: 0, total: 1 },
    fetched_at: 1_700_000_000,
    sources: [source()],
    enabled: true,
    interval_seconds: 300,
    server_time: 1_700_000_030,
    ...overrides,
  };
}

describe("UpstreamStatusPage", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("渲染概览条与源的档位、官方页外链", async () => {
    stubFetch(payload({
      summary: { level: "major", operational: 1, abnormal: 1, unknown: 0, total: 2 },
      sources: [
        source(),
        source({ key: "openai", label: "OpenAI", scope: "model",
                 page_url: "https://status.openai.com", level: "major",
                 related_providers: [] }),
      ],
    }));
    renderPage(<UpstreamStatusPage />, { username: "root", is_admin: true });
    await settle();

    expect(screen.getByTestId("upstream-status-page")).toBeInTheDocument();
    const overview = screen.getByTestId("upstream-overview");
    // 整体档位取最差源
    expect(overview).toHaveTextContent("重大故障");
    expect(overview).toHaveTextContent("最差的源决定整体档位（共 2 个源）");
    // 分布条按档位给出文字图例（颜色不单独表意）
    expect(screen.getByRole("img", { name: /重大故障 1，正常 1/ })).toBeInTheDocument();

    const row = screen.getByTestId("upstream-source-openai");
    expect(row).toHaveTextContent("OpenAI");
    expect(row).toHaveTextContent("重大故障");
    const link = screen.getByTestId("upstream-link-openai");
    expect(link).toHaveAttribute("href", "https://status.openai.com");
    expect(link).toHaveAttribute("target", "_blank");
  });

  it("行内摘要说清状态，不必展开", async () => {
    stubFetch(payload({
      sources: [source({
        components: [
          { name: "Gateway", level: "partial", detail: null },
          { name: "Embeddings", level: "operational", detail: null },
        ],
        incidents: [
          { title: "Elevated errors", level: "major", ongoing: true,
            updated_at: null, url: null, detail: null },
        ],
      })],
    }));
    renderPage(<UpstreamStatusPage />, { username: "root", is_admin: true });
    await settle();

    const row = screen.getByTestId("upstream-source-kilo");
    expect(row).toHaveTextContent("2 项明细，1 项异常");
    expect(row).toHaveTextContent("1 条进行中事故");
  });

  it("渠道组排在模型组之前，两组标题都出现", async () => {
    stubFetch(payload({
      sources: [
        source(),
        source({ key: "openai", label: "OpenAI", scope: "model", related_providers: [] }),
      ],
    }));
    renderPage(<UpstreamStatusPage />, { username: "root", is_admin: true });
    await settle();

    const channel = screen.getByText("渠道（与本站入口一一对应）");
    const model = screen.getByText("模型 / 上游（经渠道间接使用）");
    // compareDocumentPosition：渠道标题必须先于模型标题出现
    expect(channel.compareDocumentPosition(model) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy();
  });

  it("异常源默认展开、正常源默认折叠，可手动切换", async () => {
    stubFetch(payload({
      sources: [
        source(),
        source({ key: "openai", label: "OpenAI", scope: "model",
                 related_providers: [], level: "degraded" }),
      ],
    }));
    renderPage(<UpstreamStatusPage />, { username: "root", is_admin: true });
    await settle();

    const kilo = screen.getByTestId("upstream-row-toggle-kilo");
    const openai = screen.getByTestId("upstream-row-toggle-openai");
    expect(kilo).toHaveAttribute("aria-expanded", "false");
    expect(openai).toHaveAttribute("aria-expanded", "true");

    await userEvent.click(kilo);
    expect(kilo).toHaveAttribute("aria-expanded", "true");
    await userEvent.click(openai);
    expect(openai).toHaveAttribute("aria-expanded", "false");
  });

  it("需要关注条只列非正常源，点击跳到对应行锚点", async () => {
    stubFetch(payload({
      sources: [
        source(),
        source({ key: "openai", label: "OpenAI", scope: "model", level: "major",
                 related_providers: [],
                 components: [{ name: "Compliance API", level: "degraded", detail: null }] }),
      ],
    }));
    renderPage(<UpstreamStatusPage />, { username: "root", is_admin: true });
    await settle();

    const attention = screen.getByTestId("upstream-attention");
    // 正常源不进这一条
    expect(attention).not.toHaveTextContent("Kilo");
    expect(attention).toHaveTextContent("OpenAI");
    // 最差组件名作为原因
    expect(attention).toHaveTextContent("Compliance API");
    expect(screen.getByRole("link", { name: /OpenAI/ }))
      .toHaveAttribute("href", "#upstream-row-openai");
  });

  it("全部正常时不渲染需要关注条", async () => {
    stubFetch(payload());
    renderPage(<UpstreamStatusPage />, { username: "root", is_admin: true });
    await settle();

    expect(screen.queryByTestId("upstream-attention")).not.toBeInTheDocument();
  });

  it("组件明细与事故列表：进行中 / 已恢复、原文链接", async () => {
    stubFetch(payload({
      sources: [source({
        level: "partial",
        components: [
          { name: "Gateway", level: "partial", detail: "TTFT 2.3s" },
          { name: "Embeddings", level: "operational", detail: null },
        ],
        incidents: [
          { title: "Elevated errors", level: "major", ongoing: true,
            updated_at: 1_700_000_000, url: "https://stspg.io/x", detail: "正在调查" },
          { title: "旧事故", level: "degraded", ongoing: false,
            updated_at: null, url: null, detail: null },
        ],
      })],
    }));
    renderPage(<UpstreamStatusPage />, { username: "root", is_admin: true });
    await settle();

    const row = screen.getByTestId("upstream-source-kilo");
    expect(row).toHaveTextContent("Gateway");
    expect(row).toHaveTextContent("TTFT 2.3s");
    // 进行中事故默认铺开，已恢复的折在计数按钮后面
    expect(row).toHaveTextContent("Elevated errors");
    expect(row).toHaveTextContent("进行中");
    expect(row).toHaveTextContent("正在调查");
    expect(row).not.toHaveTextContent("旧事故");
    expect(screen.getByRole("link", { name: /原文/ }))
      .toHaveAttribute("href", "https://stspg.io/x");

    await userEvent.click(screen.getByTestId("upstream-recovered-toggle-kilo"));
    expect(row).toHaveTextContent("旧事故");
    expect(row).toHaveTextContent("已恢复");
  });

  it("事故链接只放行 http(s)：javascript: 协议不渲染成链接", async () => {
    stubFetch(payload({
      sources: [source({
        level: "major",
        incidents: [{ title: "恶意链接", level: "major", ongoing: true,
                      updated_at: 1_700_000_000,
                      url: "javascript:alert(1)", detail: null }],
      })],
    }));
    renderPage(<UpstreamStatusPage />, { username: "root", is_admin: true });
    await settle();

    // 上游下发的 url 直接进 href 就能执行脚本，必须过协议白名单
    expect(screen.queryByRole("link", { name: /原文/ })).toBeNull();
  });

  it("没有进行中事故时明确说明，而不是留白", async () => {
    stubFetch(payload({
      sources: [source({
        level: "degraded",
        incidents: [{ title: "旧事故", level: "degraded", ongoing: false,
                      updated_at: null, url: null, detail: null }],
      })],
    }));
    renderPage(<UpstreamStatusPage />, { username: "root", is_admin: true });
    await settle();

    expect(screen.getByText("无进行中事故。")).toBeInTheDocument();
  });

  it("组件明细超过上限时截断并给出「显示全部」", async () => {
    const components = Array.from({ length: 8 }, (_, index) => ({
      name: `Item ${index}`,
      level: "operational" as const,
      detail: null,
    }));
    stubFetch(payload({
      sources: [source({ level: "degraded", components })],
    }));
    renderPage(<UpstreamStatusPage />, { username: "root", is_admin: true });
    await settle();

    // 上限 6 项：第 7 项默认不渲染
    expect(screen.getByText("Item 0")).toBeInTheDocument();
    expect(screen.queryByText("Item 6")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "显示全部 8 项" }));
    expect(screen.getByText("Item 7")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "只看前 6 项" }));
    expect(screen.queryByText("Item 7")).not.toBeInTheDocument();
  });

  it("截断时异常项排前面，不会被折在「显示全部」后面", async () => {
    // 异常项排在源侧顺序的第 8 位：若按原始顺序截断就会被藏起来
    const components = [
      ...Array.from({ length: 7 }, (_, index) => ({
        name: `Item ${index}`,
        level: "operational" as const,
        detail: null,
      })),
      { name: "Broken", level: "major" as const, detail: null },
    ];
    stubFetch(payload({ sources: [source({ level: "major", components })] }));
    renderPage(<UpstreamStatusPage />, { username: "root", is_admin: true });
    await settle();

    // 用 within 限定在行内：需要关注条也会出现该组件名
    const row = within(screen.getByTestId("upstream-source-kilo"));
    expect(row.getByText("Broken")).toBeInTheDocument();
    expect(row.queryByText("Item 5")).not.toBeInTheDocument();
  });

  it("抓取失败的源显示错误原因，而不是空行", async () => {
    stubFetch(payload({
      summary: { level: "unknown", operational: 0, abnormal: 1, unknown: 1, total: 1 },
      sources: [source({ level: "unknown", error: "连接超时" })],
    }));
    renderPage(<UpstreamStatusPage />, { username: "root", is_admin: true });
    await settle();

    const error = screen.getByTestId("upstream-error-kilo");
    expect(error).toHaveTextContent("连接超时");
    // 行内摘要给出结论（长错误原文只在展开态，不重复铺两遍）
    expect(screen.getByTestId("upstream-source-kilo")).toHaveTextContent("本次抓取失败");
  });

  it("源侧声明数据过期时给出提示", async () => {
    stubFetch(payload({ sources: [source({ source_stale: true })] }));
    renderPage(<UpstreamStatusPage />, { username: "root", is_admin: true });
    await settle();

    expect(screen.getByTestId("upstream-source-kilo")).toHaveTextContent("源侧数据过期");
  });

  it("「只看异常」默认关闭；开启后只留非正常源", async () => {
    stubFetch(payload({
      sources: [
        source(),
        source({ key: "openai", label: "OpenAI", scope: "model",
                 related_providers: [], level: "major" }),
      ],
    }));
    renderPage(<UpstreamStatusPage />, { username: "root", is_admin: true });
    await settle();

    // 默认两个都在
    expect(screen.getByTestId("upstream-source-kilo")).toBeInTheDocument();
    expect(screen.getByTestId("upstream-source-openai")).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("upstream-abnormal-toggle"));

    await waitFor(() =>
      expect(screen.queryByTestId("upstream-source-kilo")).not.toBeInTheDocument());
    expect(screen.getByTestId("upstream-source-openai")).toBeInTheDocument();
  });

  it("开启「只看异常」后需要关注条不再重复同一份内容，并播报隐藏条数", async () => {
    stubFetch(payload({
      sources: [
        source(),
        source({ key: "openai", label: "OpenAI", scope: "model",
                 related_providers: [], level: "major" }),
      ],
    }));
    renderPage(<UpstreamStatusPage />, { username: "root", is_admin: true });
    await settle();

    expect(screen.getByTestId("upstream-attention")).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("upstream-abnormal-toggle"));

    expect(screen.queryByTestId("upstream-attention")).not.toBeInTheDocument();
    expect(screen.getByTestId("upstream-hidden-count")).toHaveTextContent("已隐藏 1 个正常源");
  });

  it("刷新按钮触发重新抓取", async () => {
    const spy = stubFetch(payload());
    renderPage(<UpstreamStatusPage />, { username: "root", is_admin: true });
    await settle();

    const before = spy.mock.calls.length;
    await userEvent.click(screen.getByTestId("upstream-refresh"));

    await waitFor(() => expect(spy.mock.calls.length).toBeGreaterThan(before));
  });

  it("全部正常时开启「只看异常」显示空态而非空白", async () => {
    stubFetch(payload());
    renderPage(<UpstreamStatusPage />, { username: "root", is_admin: true });
    await settle();

    await userEvent.click(screen.getByTestId("upstream-abnormal-toggle"));

    expect(await screen.findByTestId("no-upstream-abnormal")).toBeInTheDocument();
  });

  it("尚未抓取（无源）时显示引导空态", async () => {
    stubFetch(payload({
      summary: { level: "unknown", operational: 0, abnormal: 0, unknown: 0, total: 0 },
      fetched_at: null,
      sources: [],
    }));
    renderPage(<UpstreamStatusPage />, { username: "root", is_admin: true });
    await settle();

    expect(screen.getByTestId("no-upstream-status")).toBeInTheDocument();
    expect(screen.getByTestId("upstream-status-page")).toHaveTextContent("尚未抓取");
  });

  it("任务关闭时给出过时快照提示", async () => {
    stubFetch(payload({ enabled: false }));
    renderPage(<UpstreamStatusPage />, { username: "root", is_admin: true });
    await settle();

    expect(screen.getByTestId("upstream-disabled")).toBeInTheDocument();
  });

  it("请求失败显示错误提示", async () => {
    stubFetch({ error: { code: "boom", message: "挂了" } }, 500);
    renderPage(<UpstreamStatusPage />, { username: "root", is_admin: true });
    await settle();

    expect(await screen.findByTestId("upstream-status-error")).toBeInTheDocument();
  });
});
