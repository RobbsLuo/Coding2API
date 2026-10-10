import { fireEvent, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ModelCatalogPage } from "../ModelCatalogPage";
import { renderPage, settle } from "./helpers";

function stubFetch(body: unknown, status = 200) {
  const spy = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", spy);
  return spy;
}

const CATALOG = {
  count: 2,
  currency: "USD",
  usd_cny_rate: 7,
  saved_at: 1_700_000_000,
  models: [
    {
      id: "a-model",
      name: "A Model",
      provider: "alpha",
      family: null,
      knowledge: "2024-01-01",
      release_date: "2024-02-01",
      context: 128000,
      max_output: 16000,
      input_modalities: ["text", "image"],
      output_modalities: ["text"],
      attachment: true,
      reasoning: false,
      tool_call: true,
      structured_output: false,
      open_weights: false,
      input: 0.5,
      output: 1.5,
      cache_read: 0.05,
      cache_write: null,
    },
    {
      id: "glm-5.2",
      name: null,
      provider: "zhipuai",
      family: "glm",
      knowledge: null,
      release_date: null,
      context: 200000,
      max_output: 64000,
      input_modalities: ["text"],
      output_modalities: ["text"],
      attachment: false,
      reasoning: true,
      tool_call: true,
      structured_output: true,
      open_weights: true,
      input: 1.0,
      output: 2.0,
      cache_read: 0.1,
      cache_write: 1.25,
    },
  ],
};

describe("ModelCatalogPage（模型列表）", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("渲染模型明细：名称 / id / 渠道 / 模态 / 能力 / 知识与刊例价", async () => {
    stubFetch(CATALOG);
    renderPage(<ModelCatalogPage />);
    await settle();

    expect(screen.getByTestId("model-catalog-table")).toBeInTheDocument();
    const row = screen.getByTestId("model-row-a-model");
    expect(row).toHaveTextContent("A Model");
    expect(row).toHaveTextContent("a-model");
    expect(row).toHaveTextContent("alpha");
    expect(row).toHaveTextContent("文本 · 图片");
    expect(row).toHaveTextContent("工具调用 · 附件");
    expect(row).toHaveTextContent("2024-01-01");
    // 价格合并为一列两行：上行「输入 · 输出」，下行「缓存读 · 缓存写」（缺失显示 —）
    expect(row).toHaveTextContent("输入 $0.5 · 输出 $1.5");
    expect(row).toHaveTextContent("缓存读 $0.05 · 缓存写 —");
    expect(screen.getByTestId("model-catalog-page")).toHaveTextContent("1 USD = 7 CNY");
    expect(screen.getByTestId("model-catalog-page")).toHaveTextContent("模型数");
  });

  it("名称缺失时回退显示 id，缓存写价照常展示", async () => {
    stubFetch(CATALOG);
    renderPage(<ModelCatalogPage />);
    await settle();

    const row = screen.getByTestId("model-row-glm-5.2");
    expect(row).toHaveTextContent("glm-5.2");
    expect(row).toHaveTextContent("推理 · 工具调用 · 结构化输出 · 开放权重");
    // 上下文 / 输出分两行展示
    expect(row).toHaveTextContent("20万");
    expect(row).toHaveTextContent("6.4万");
    expect(row).toHaveTextContent("输入 $1 · 输出 $2");
    expect(row).toHaveTextContent("缓存读 $0.1 · 缓存写 $1.25");
  });

  it("搜索按 id / 名称 / 渠道过滤，无匹配显示空态", async () => {
    stubFetch(CATALOG);
    renderPage(<ModelCatalogPage />);
    await settle();

    fireEvent.change(screen.getByTestId("model-search"), { target: { value: "glm" } });
    expect(screen.getByTestId("model-row-glm-5.2")).toBeInTheDocument();
    expect(screen.queryByTestId("model-row-a-model")).not.toBeInTheDocument();

    // 按展示名搜（大写差异走小写归一）
    fireEvent.change(screen.getByTestId("model-search"), { target: { value: "a model" } });
    expect(screen.getByTestId("model-row-a-model")).toBeInTheDocument();
    expect(screen.queryByTestId("model-row-glm-5.2")).not.toBeInTheDocument();

    // 按渠道搜
    fireEvent.change(screen.getByTestId("model-search"), { target: { value: "zhipuai" } });
    expect(screen.getByTestId("model-row-glm-5.2")).toBeInTheDocument();

    fireEvent.change(screen.getByTestId("model-search"), { target: { value: "zzz" } });
    expect(screen.getByTestId("no-model-match")).toBeInTheDocument();
  });

  it("切到人民币按汇率折算", async () => {
    stubFetch(CATALOG);
    renderPage(<ModelCatalogPage />);
    await settle();

    fireEvent.click(screen.getByTestId("model-currency-tabs-CNY"));
    // 1.0 USD × 7 = ¥7；0.5 × 7 = ¥3.5
    expect(screen.getByTestId("model-row-glm-5.2")).toHaveTextContent("¥7");
    expect(screen.getByTestId("model-row-a-model")).toHaveTextContent("¥3.5");
  });

  it("超过一页时分页，翻页只渲染当前页", async () => {
    const many = Array.from({ length: 60 }, (_, i) => ({
      ...CATALOG.models[0],
      id: `m-${String(i).padStart(2, "0")}`,
      name: `Model ${i}`,
    }));
    stubFetch({ ...CATALOG, count: many.length, models: many });
    renderPage(<ModelCatalogPage />);
    await settle();

    // 默认每页 50：第一页 50 行，第二页 10 行
    expect(screen.getByTestId("model-page-info")).toHaveTextContent("第 1 / 2 页");
    expect(screen.getAllByTestId(/^model-row-/)).toHaveLength(50);
    expect(screen.getByTestId("model-prev")).toBeDisabled();

    fireEvent.click(screen.getByTestId("model-next"));
    expect(screen.getByTestId("model-page-info")).toHaveTextContent("第 2 / 2 页");
    expect(screen.getAllByTestId(/^model-row-/)).toHaveLength(10);
    expect(screen.getByTestId("model-next")).toBeDisabled();
  });

  it("搜索后回到第一页并只渲染命中项", async () => {
    const many = Array.from({ length: 60 }, (_, i) => ({
      ...CATALOG.models[0],
      id: `m-${String(i).padStart(2, "0")}`,
      name: `Model ${i}`,
    }));
    stubFetch({ ...CATALOG, count: many.length, models: many });
    renderPage(<ModelCatalogPage />);
    await settle();

    fireEvent.click(screen.getByTestId("model-next"));
    expect(screen.getByTestId("model-page-info")).toHaveTextContent("第 2 / 2 页");

    // 改搜索词后应回到第一页（否则会停在一个已不存在的页码上）
    fireEvent.change(screen.getByTestId("model-search"), { target: { value: "Model 5" } });
    expect(screen.queryByTestId("model-page-info")).not.toBeInTheDocument();
    expect(screen.getAllByTestId(/^model-row-/).length).toBeLessThanOrEqual(50);
    expect(screen.getByTestId("model-row-m-59")).toBeInTheDocument();
  });

  it("能力分列展示三项指数，缺失显示 —", async () => {
    stubFetch({
      ...CATALOG,
      benchmark_saved_at: 1_700_000_100,
      models: [
        { ...CATALOG.models[1], benchmarks: {
          intelligence_index: 44.8, coding_index: 74.8, agentic_index: 53.1 } },
        CATALOG.models[0],
      ],
    });
    renderPage(<ModelCatalogPage />);
    await settle();

    const row = screen.getByTestId("model-row-glm-5.2");
    expect(row).toHaveTextContent("智 44.8");
    expect(row).toHaveTextContent("编 74.8");
    expect(row).toHaveTextContent("体 53.1");
    // 无分数的行只显示一个 —，不渲染三个
    const plain = screen.getByTestId("model-row-a-model");
    expect(plain.querySelectorAll("div").length).toBeGreaterThan(0);
    expect(plain).not.toHaveTextContent("智 ");
    // 能力分快照时刻单独展示（与目录快照分开）
    expect(screen.getByTestId("model-catalog-page")).toHaveTextContent("能力分更新");
  });

  it("无能力分快照时指标卡显示 —", async () => {
    stubFetch({ ...CATALOG, benchmark_saved_at: null });
    renderPage(<ModelCatalogPage />);
    await settle();
    expect(screen.getByTestId("model-catalog-page")).toHaveTextContent("尚无能力分快照");
  });

  it("无目录快照时显示空态", async () => {
    stubFetch({ ...CATALOG, count: 0, models: [], saved_at: null });
    renderPage(<ModelCatalogPage />);
    await settle();
    expect(screen.getByTestId("no-model-catalog")).toBeInTheDocument();
  });

  it("加载失败显示错误提示", async () => {
    stubFetch({ error: { code: "boom", message: "x" } }, 500);
    renderPage(<ModelCatalogPage />);
    await settle();
    expect(screen.getByTestId("model-catalog-error")).toBeInTheDocument();
  });
});
