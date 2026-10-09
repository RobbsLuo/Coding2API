import { fireEvent, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PriceCatalogPage } from "../PriceCatalogPage";
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

const PRICING = {
  count: 2,
  currency: "USD",
  usd_cny_rate: 7,
  saved_at: 1_700_000_000,
  models: [
    { model: "a-model", input: 0.5, output: 1.5, cache_read: 0.05 },
    { model: "glm-5.2", input: 1.0, output: 2.0, cache_read: 0.1 },
  ],
};

describe("PriceCatalogPage", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("渲染价表行：输入 / 输出 / 缓存，美元为单位", async () => {
    stubFetch(PRICING);
    renderPage(<PriceCatalogPage />);
    await settle();

    expect(screen.getByTestId("pricing-table")).toBeInTheDocument();
    const row = screen.getByTestId("price-row-a-model");
    expect(row).toHaveTextContent("$0.5");
    expect(row).toHaveTextContent("$1.5");
    expect(row).toHaveTextContent("$0.05");
    expect(screen.getByTestId("pricing-page")).toHaveTextContent("1 USD = 7 CNY");
  });

  it("搜索按模型 id 过滤，无匹配显示空态", async () => {
    stubFetch(PRICING);
    renderPage(<PriceCatalogPage />);
    await settle();

    fireEvent.change(screen.getByTestId("price-search"), { target: { value: "glm" } });
    expect(screen.getByTestId("price-row-glm-5.2")).toBeInTheDocument();
    expect(screen.queryByTestId("price-row-a-model")).not.toBeInTheDocument();

    fireEvent.change(screen.getByTestId("price-search"), { target: { value: "zzz" } });
    expect(screen.getByTestId("no-price-match")).toBeInTheDocument();
  });

  it("切到人民币按汇率折算", async () => {
    stubFetch(PRICING);
    renderPage(<PriceCatalogPage />);
    await settle();

    fireEvent.click(screen.getByTestId("price-currency-tabs-CNY"));
    // 1.0 USD × 7 = ¥7
    expect(screen.getByTestId("price-row-glm-5.2")).toHaveTextContent("¥7");
    expect(screen.getByTestId("price-row-a-model")).toHaveTextContent("¥3.5");
  });

  it("无价表快照时显示空态", async () => {
    stubFetch({ ...PRICING, count: 0, models: [], saved_at: null });
    renderPage(<PriceCatalogPage />);
    await settle();
    expect(screen.getByTestId("no-pricing")).toBeInTheDocument();
  });

  it("加载失败显示错误提示", async () => {
    stubFetch({ error: { code: "boom", message: "x" } }, 500);
    renderPage(<PriceCatalogPage />);
    await settle();
    expect(screen.getByTestId("pricing-error")).toBeInTheDocument();
  });
});
