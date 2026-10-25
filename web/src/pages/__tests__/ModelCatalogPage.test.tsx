import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ModelCatalogPage } from "../ModelCatalogPage";
import { renderPage, settle, userEvent } from "@/test-utils";

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

/**
 * 模拟服务端的目录端点：筛选（q / only_available）+ 排序 + 分页都在「服务端」，
 * 与 `src/api/admin_model_catalog.py` 同口径。前端已改为服务端分页，静态响应不再
 * 能验证翻页与筛选——必须让 mock 读懂 URL 上的参数。
 */
function stubCatalog(models: Array<Record<string, unknown>>, status = 200) {
  const spy = vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://localhost");
    const q = (url.searchParams.get("q") ?? "").toLowerCase();
    const onlyAvailable = url.searchParams.get("only_available") === "true";
    const onlySuspected = url.searchParams.get("only_suspected") === "true";
    const limit = Number(url.searchParams.get("limit") ?? 50);
    const offset = Number(url.searchParams.get("offset") ?? 0);
    const sort = url.searchParams.get("sort") ?? "id";
    const order = url.searchParams.get("order") ?? "asc";
    let rows = models.filter((row) => {
      if (onlyAvailable && !row.available) return false;
      if (onlySuspected && !row.suspected) return false;
      if (!q) return true;
      return String(row.id).toLowerCase().includes(q)
        || String(row.name ?? "").toLowerCase().includes(q);
    });
    rows = [...rows].sort((left, right) => {
      const a = left[sort] as string | number | null;
      const b = right[sort] as string | number | null;
      if (a === b) return 0;
      if (a === null || a === undefined) return 1;
      if (b === null || b === undefined) return -1;
      const cmp = a < b ? -1 : 1;
      return order === "desc" ? -cmp : cmp;
    });
    const total = rows.length;
    return new Response(JSON.stringify({
      models: rows.slice(offset, offset + limit),
      count: models.length,
      total,
      currency: "USD",
      usd_cny_rate: 7,
      saved_at: 1_700_000_000,
      availability_days: 7,
    }), { status, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", spy);
  return spy;
}

const MODELS = [
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
    available: true,
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
    available: false,
  },
];

const CATALOG = { count: 2, models: MODELS };

describe("ModelCatalogPage（模型列表）", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("渲染模型明细：名称 / id / 模态 / 能力 / 知识与刊例价", async () => {
    stubCatalog(MODELS);
    renderPage(<ModelCatalogPage />);
    await settle();

    expect(screen.getByTestId("model-catalog-table")).toBeInTheDocument();
    const row = screen.getByTestId("model-row-a-model");
    expect(row).toHaveTextContent("A Model");
    expect(row).toHaveTextContent("a-model");
    expect(row).toHaveTextContent("文本 · 图片");
    expect(row).toHaveTextContent("工具调用 · 附件");
    expect(row).toHaveTextContent("2024-01-01");
    // OpenRouter 不提供原厂发布时间：只留知识截止一列，不渲染发布日期（全表恒 —）
    expect(screen.getByTestId("sort-knowledge")).toBeInTheDocument();
    expect(screen.queryByTestId("sort-release_date")).not.toBeInTheDocument();
    expect(screen.queryByText("知识 / 发布")).not.toBeInTheDocument();
    expect(row).not.toHaveTextContent("2024-02-01");   // release_date 不展示
    // 价格合并为一列两行：上行「输入 · 输出」，下行「缓存读 · 缓存写」（缺失显示 —）
    expect(row).toHaveTextContent("输入 $0.5 · 输出 $1.5");
    expect(row).toHaveTextContent("缓存读 $0.05 · 缓存写 —");
    expect(screen.getByTestId("model-catalog-page")).toHaveTextContent("1 USD = 7 CNY");
    // 「提供方」列与「模型数」指标卡已移除（provider 恒为 id 前缀，信息重复）
    expect(screen.queryByText("提供方")).not.toBeInTheDocument();
    expect(screen.queryByTestId("sort-provider")).not.toBeInTheDocument();
    expect(screen.queryByText("模型数")).not.toBeInTheDocument();
  });

  it("名称缺失时回退显示 id，缓存写价照常展示", async () => {
    stubCatalog(MODELS);
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

  it("搜索按 id / 名称过滤（服务端），无匹配显示空态", async () => {
    stubCatalog(MODELS);
    renderPage(<ModelCatalogPage />);
    await settle();

    // 搜索词去抖 + 服务端取数：等旧行消失，避免在 keepPreviousData 的旧页上判定
    fireEvent.change(screen.getByTestId("model-search"), { target: { value: "glm" } });
    await waitFor(() =>
      expect(screen.queryByTestId("model-row-a-model")).not.toBeInTheDocument());
    expect(screen.getByTestId("model-row-glm-5.2")).toBeInTheDocument();

    // 按展示名搜（大写差异走小写归一）
    fireEvent.change(screen.getByTestId("model-search"), { target: { value: "a model" } });
    await waitFor(() =>
      expect(screen.queryByTestId("model-row-glm-5.2")).not.toBeInTheDocument());
    expect(screen.getByTestId("model-row-a-model")).toBeInTheDocument();

    fireEvent.change(screen.getByTestId("model-search"), { target: { value: "zzz" } });
    await waitFor(() => expect(screen.getByTestId("no-model-match")).toBeInTheDocument());
  });

  it("按 id 的厂商前缀搜（provider 列已移除，前缀仍在 id 里）", async () => {
    stubCatalog([
      { ...MODELS[0], id: "zhipuai/glm-5.3", name: "GLM 5.3" },
      { ...MODELS[0], id: "moonshotai/kimi-k3", name: "Kimi K3" },
    ]);
    renderPage(<ModelCatalogPage />);
    await settle();

    fireEvent.change(screen.getByTestId("model-search"), { target: { value: "zhipuai/" } });
    await waitFor(() =>
      expect(screen.queryByTestId("model-row-moonshotai/kimi-k3")).not.toBeInTheDocument());
    expect(screen.getByTestId("model-row-zhipuai/glm-5.3")).toBeInTheDocument();
  });

  it("切到人民币按汇率折算", async () => {
    stubCatalog(MODELS);
    renderPage(<ModelCatalogPage />);
    await settle();

    fireEvent.click(screen.getByTestId("model-currency-tabs-CNY"));
    // 1.0 USD × 7 = ¥7；0.5 × 7 = ¥3.5
    expect(screen.getByTestId("model-row-glm-5.2")).toHaveTextContent("¥7");
    expect(screen.getByTestId("model-row-a-model")).toHaveTextContent("¥3.5");
  });

  it("超过一页时分页，翻页只渲染当前页", async () => {
    const many = Array.from({ length: 60 }, (_, i) => ({
      ...MODELS[0],
      id: `m-${String(i).padStart(2, "0")}`,
      name: `Model ${i}`,
    }));
    stubCatalog(many);
    renderPage(<ModelCatalogPage />);
    await settle();

    // 默认每页 50：第一页 50 行，第二页 10 行
    expect(screen.getByTestId("model-page-info")).toHaveTextContent("第 1 / 2 页");
    expect(screen.getAllByTestId(/^model-row-/)).toHaveLength(50);
    expect(screen.getByTestId("model-prev")).toBeDisabled();

    fireEvent.click(screen.getByTestId("model-next"));
    // keepPreviousData：翻页期间仍显示上一页，必须等到新页行数到位
    await waitFor(() =>
      expect(screen.getAllByTestId(/^model-row-/)).toHaveLength(10));
    expect(screen.getByTestId("model-page-info")).toHaveTextContent("第 2 / 2 页");
    expect(screen.getByTestId("model-next")).toBeDisabled();
  });

  it("搜索后回到第一页并只渲染命中项", async () => {
    const many = Array.from({ length: 60 }, (_, i) => ({
      ...MODELS[0],
      id: `m-${String(i).padStart(2, "0")}`,
      name: `Model ${i}`,
    }));
    stubCatalog(many);
    renderPage(<ModelCatalogPage />);
    await settle();

    fireEvent.click(screen.getByTestId("model-next"));
    await waitFor(() =>
      expect(screen.getAllByTestId(/^model-row-/)).toHaveLength(10));

    // 改搜索词后应回到第一页（否则会停在一个已不存在的页码上）
    fireEvent.change(screen.getByTestId("model-search"), { target: { value: "Model 5" } });
    // 「Model 5」命中 Model 5 / 50-59 共 11 条 → 单页。两个条件一起等：
    // 只看页信息会在 keepPreviousData 的空页上误判，只看 m-59 又会撞上第二页残留。
    await waitFor(() => {
      expect(screen.getByTestId("model-page-info")).toHaveTextContent("第 1 / 1 页");
      expect(screen.getByTestId("model-row-m-59")).toBeInTheDocument();
    });
    expect(screen.getAllByTestId(/^model-row-/).length).toBeLessThanOrEqual(50);
  });

  it("能力分列展示三项指数，缺失显示 —", async () => {
    stubCatalog([
      { ...MODELS[1], benchmarks: {
        intelligence_index: 44.8, coding_index: 74.8, agentic_index: 53.1 } },
      MODELS[0],
    ]);
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
    // 能力分来源指标卡（与价格同一次抓取）
    expect(screen.getByTestId("model-catalog-page")).toHaveTextContent("能力分来源");
  });

  it("无能力分快照时指标卡显示 —", async () => {
    stubFetch({ ...CATALOG, total: 2, saved_at: null });
    renderPage(<ModelCatalogPage />);
    await settle();
    expect(screen.getByTestId("model-catalog-page")).toHaveTextContent(
      "尚无目录快照，成本与能力分将显示 —");
  });

  it("无目录快照时显示空态", async () => {
    stubFetch({ ...CATALOG, count: 0, total: 0, models: [], saved_at: null });
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

  it("「只看当前渠道」默认关闭，开启后只留 available 的模型", async () => {
    stubCatalog(MODELS);
    renderPage(<ModelCatalogPage />);
    await settle();

    // 默认关闭：两条都展示
    const toggle = screen.getByTestId("model-available-toggle");
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(screen.getByTestId("model-row-a-model")).toBeInTheDocument();
    expect(screen.getByTestId("model-row-glm-5.2")).toBeInTheDocument();

    // 开启：只留 available=true 的 a-model，标题计数同步收窄（命中 1 / 目录 2）
    fireEvent.click(toggle);
    await waitFor(() =>
      expect(screen.queryByTestId("model-row-glm-5.2")).not.toBeInTheDocument());
    expect(toggle).toHaveAttribute("aria-checked", "true");
    expect(screen.getByTestId("model-row-a-model")).toBeInTheDocument();
    expect(screen.getByText("模型列表（1 / 2）")).toBeInTheDocument();

    // 关闭：恢复两条
    fireEvent.click(toggle);
    await waitFor(() =>
      expect(screen.getByTestId("model-row-glm-5.2")).toBeInTheDocument());
  });

  it("开启过滤但无任何 available 模型时显示渠道空态（非搜索空态）", async () => {
    stubCatalog(MODELS.map((row) => ({ ...row, available: false })));
    renderPage(<ModelCatalogPage />);
    await settle();

    fireEvent.click(screen.getByTestId("model-available-toggle"));
    await waitFor(() => expect(screen.getByTestId("no-model-match")).toHaveTextContent(
      "当前渠道没有可用模型"));
  });

  it("吸顶表头不透明：滚动时行内容不从表头底下透出", async () => {
    stubCatalog(MODELS);
    renderPage(<ModelCatalogPage />);
    await settle();

    // 表头 sticky 且 th 带不透明底色（基类 tr 的 bg-muted/40 是半透明的，
    // 不覆盖会在滚动时透出后面的行）
    const thead = screen.getByTestId("model-catalog-table").querySelector("thead")!;
    expect(thead).toHaveClass("sticky");
    expect(thead).toHaveClass("[&_th]:bg-muted");
  });

  it("能力分列可排序：点列头下发 sort=intelligence（首点降序）", async () => {
    const spy = stubCatalog(MODELS);
    renderPage(<ModelCatalogPage />);
    await settle();

    const urls = () => spy.mock.calls.map((call) => String(call[0]));
    const catalogUrls = () => urls().filter((url) => url.includes("/api/model-catalog"));
    // 默认按 id 升序，不带 intelligence
    expect(catalogUrls()[0]).toContain("sort=id");

    await userEvent.click(screen.getByTestId("sort-intelligence"));
    await settle();
    expect(catalogUrls().at(-1)!).toContain("sort=intelligence");
    expect(catalogUrls().at(-1)!).toContain("order=desc");   // 分数类列首点降序（先看最高）

    // 再点同列：升序
    await userEvent.click(screen.getByTestId("sort-intelligence"));
    await settle();
    expect(catalogUrls().at(-1)!).toContain("order=asc");
  });

  // ------------------------------------------------ 疑似下线 / 一键黑名单

  const SUSPECTED = {
    ...MODELS[1],
    suspected: {
      provider: "trae",
      model: "glm-5.2",
      failures: 3,
      last_failed_at: 1_700_000_000,
      blocked: false,
    },
  };

  it("「疑似下线」筛选只留窗口内有模型级失败的行", async () => {
    stubCatalog([MODELS[0], SUSPECTED]);
    renderPage(<ModelCatalogPage />);
    await settle();

    // 默认关闭：两条都在；可用性列只对有失败记录的行渲染
    expect(screen.getByTestId("model-row-a-model")).toBeInTheDocument();
    expect(screen.getByTestId("model-availability-glm-5.2")).toHaveTextContent(
      "近 7 天失败 3 次");
    expect(screen.queryByTestId("model-availability-a-model")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("model-suspected-toggle"));
    await waitFor(() =>
      expect(screen.queryByTestId("model-row-a-model")).not.toBeInTheDocument());
    expect(screen.getByTestId("model-row-glm-5.2")).toBeInTheDocument();
    expect(screen.getByText("模型列表（1 / 2）")).toBeInTheDocument();
  });

  it("「疑似下线」筛选无命中时给出专属空态", async () => {
    stubCatalog(MODELS);
    renderPage(<ModelCatalogPage />);
    await settle();

    fireEvent.click(screen.getByTestId("model-suspected-toggle"));
    await waitFor(() => expect(screen.getByTestId("no-model-match")).toHaveTextContent(
      "最近 7 天没有模型级失败"));
  });

  it("「加入黑名单」把模型追加进 MODEL_BLOCKLIST（读现值再写，不覆盖已有项）", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const spy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, init });
      if (url === "/api/settings" && init?.method === "PUT") {
        return new Response(JSON.stringify({ settings: [], overridden: 2, groups: [] }),
          { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (url === "/api/settings") {
        return new Response(JSON.stringify({
          settings: [{ key: "model_blocklist", value: "custom_model_*" }],
          overridden: 1,
          groups: [],
        }), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      return new Response(JSON.stringify({
        models: [SUSPECTED], count: 1, total: 1, currency: "USD",
        usd_cny_rate: 7, saved_at: 1_700_000_000, availability_days: 7,
      }), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    vi.stubGlobal("fetch", spy);
    renderPage(<ModelCatalogPage />);
    await settle();

    fireEvent.click(screen.getByTestId("model-block-glm-5.2"));
    await waitFor(() => expect(screen.getByTestId("model-blocklist-toast"))
      .toHaveTextContent("已把 glm-5.2 加入黑名单"));

    const put = calls.find((call) => call.init?.method === "PUT");
    expect(put).toBeDefined();
    expect(JSON.parse(String(put!.init!.body))).toEqual({
      values: { model_blocklist: "custom_model_*,glm-5.2" },
    });
  });

  it("已命中黑名单的行按钮置灰（避免重复写审计）", async () => {
    stubCatalog([{ ...SUSPECTED, suspected: { ...SUSPECTED.suspected, blocked: true } }]);
    renderPage(<ModelCatalogPage />);
    await settle();

    expect(screen.getByTestId("model-availability-glm-5.2")).toHaveTextContent("已屏蔽");
    expect(screen.getByTestId("model-block-glm-5.2")).toBeDisabled();
  });

  it("加入黑名单失败时给出错误提示", async () => {
    const spy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "PUT") {
        return new Response(JSON.stringify({
          error: { code: "forbidden", message: "admin only" },
        }), { status: 403, headers: { "Content-Type": "application/json" } });
      }
      if (String(input) === "/api/settings") {
        return new Response(JSON.stringify({
          settings: [{ key: "model_blocklist", value: "" }],
          overridden: 0, groups: [],
        }), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      return new Response(JSON.stringify({
        models: [SUSPECTED], count: 1, total: 1, currency: "USD",
        usd_cny_rate: 7, saved_at: 1_700_000_000, availability_days: 7,
      }), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    vi.stubGlobal("fetch", spy);
    renderPage(<ModelCatalogPage />);
    await settle();

    fireEvent.click(screen.getByTestId("model-block-glm-5.2"));
    await waitFor(() => expect(screen.getByTestId("model-blocklist-toast"))
      .toHaveTextContent("加入黑名单失败"));
  });

  // ------------------------------------------------------ 主动探活 / 人工接管

  const PROBES = {
    rows: [
      {
        provider: "trae", model: "glm-5.2", credentials: 2, until: 1_700_003_600,
        hits: 4, state: "failed", failures: 2, last_probe_at: 1_700_000_000,
        last_ok_at: 0, last_error: "HTTP 404: model not found", manual: "",
      },
      {
        provider: "trae", model: "kimi-k3", credentials: 0, until: 0, hits: 0,
        state: "ok", failures: 0, last_probe_at: 1_700_000_000,
        last_ok_at: 1_700_000_000, last_error: "", manual: "",
      },
      {
        provider: "codebuddy", model: "seed-2.1", credentials: 1, until: 1_700_003_600,
        hits: 1, state: "inconclusive", failures: 0, last_probe_at: 1_700_000_000,
        last_ok_at: 0, last_error: "上游未返回任何内容", manual: "manual",
      },
      {
        provider: "trae", model: "never-probed", credentials: 1, until: 1_700_003_600,
        hits: 1, state: "", failures: 0, last_probe_at: 0,
        last_ok_at: 0, last_error: "", manual: "",
      },
    ],
  };

  /** 目录 + 探活三类端点一起 mock（探活写接口按 body 记录调用）。 */
  function stubProbePage(
    probes: unknown = PROBES,
    handler?: (url: string, init?: RequestInit) => Response | undefined,
  ) {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const spy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, init });
      const custom = handler?.(url, init);
      if (custom) return custom;
      return new Response(JSON.stringify({
        models: MODELS, count: 2, total: 2, currency: "USD",
        usd_cny_rate: 7, saved_at: 1_700_000_000, availability_days: 7,
        ...(url.includes("/api/models/probes") ? (probes as object) : {}),
      }), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    vi.stubGlobal("fetch", spy);
    return calls;
  }

  it("探活面板：三态结论 / 负缓存 / 接管标记 / 未探过都各有措辞", async () => {
    stubProbePage();
    renderPage(<ModelCatalogPage />);
    await settle();

    expect(screen.getByTestId("model-probe-summary")).toHaveTextContent(
      "共 4 个 · 仍被负缓存 3 个");

    const failed = screen.getByTestId("probe-row-trae-glm-5.2");
    expect(failed).toHaveTextContent("探活 · 确认不可用");
    expect(failed).toHaveTextContent("连续失败 2 次");
    expect(failed).toHaveTextContent("HTTP 404: model not found");
    expect(failed).toHaveTextContent("2 号 · 至");

    const recovered = screen.getByTestId("probe-row-trae-kimi-k3");
    expect(recovered).toHaveTextContent("探活 · 已恢复");
    expect(recovered).toHaveTextContent("无");

    // 「无法判定」不能被写成「失败」或「坏了」——那是额度 / 限流的措辞
    const inconclusive = screen.getByTestId("probe-row-codebuddy-seed-2.1");
    expect(inconclusive).toHaveTextContent("探活 · 无法判定");
    expect(inconclusive).not.toHaveTextContent("确认不可用");
    expect(inconclusive).toHaveTextContent("人工接管");
    expect(inconclusive).toHaveTextContent("交还自动探活");

    const never = screen.getByTestId("probe-row-trae-never-probed");
    expect(never).toHaveTextContent("探活 · 未探活");
    expect(never).toHaveTextContent("未探过");
  });

  it("探活面板：错误摘要限宽单行截断，hover 看全文", async () => {
    stubProbePage();
    renderPage(<ModelCatalogPage />);
    await settle();

    const error = screen.getByTestId("probe-error-trae-glm-5.2");
    // 折行会把整列撑宽挤掉操作列，故必须 truncate
    expect(error.className).toContain("truncate");
    expect(error).toHaveTextContent("HTTP 404: model not found");

    await userEvent.hover(error);
    const tip = await screen.findByRole("tooltip");
    expect(tip).toHaveTextContent("HTTP 404: model not found");
  });

  it("探活面板：无记录时给出空态", async () => {
    stubProbePage({ rows: [] });
    renderPage(<ModelCatalogPage />);
    await settle();

    expect(screen.getByTestId("model-probe-empty")).toHaveTextContent(
      "当前没有被负缓存锁住的模型");
  });

  it("探活面板：接口失败时给专属错误提示（不影响目录表）", async () => {
    stubProbePage(undefined, (url) => {
      if (url.includes("/api/models/probes")) {
        return new Response(JSON.stringify({
          error: { code: "forbidden", message: "operator only" },
        }), { status: 403, headers: { "Content-Type": "application/json" } });
      }
      return undefined;
    });
    renderPage(<ModelCatalogPage />);
    await settle();

    expect(screen.getByTestId("model-probe-error")).toHaveTextContent(
      "探活记录加载失败");
    expect(screen.getByTestId("model-catalog-table")).toBeInTheDocument();
  });

  it("「立即探活」成功时提示已清负缓存（POST provider/model）", async () => {
    const calls = stubProbePage(undefined, (url) =>
      url.includes("/probes/probe")
        ? new Response(JSON.stringify({
            provider: "trae", model: "glm-5.2", state: "ok", error: "",
          }), { status: 200, headers: { "Content-Type": "application/json" } })
        : undefined);
    renderPage(<ModelCatalogPage />);
    await settle();

    fireEvent.click(screen.getByTestId("probe-now-trae-glm-5.2"));
    await waitFor(() => expect(screen.getByTestId("model-probe-toast"))
      .toHaveTextContent("探活成功，已清除该渠道的负缓存"));

    const post = calls.find((call) => call.url.includes("/probes/probe"));
    expect(post?.init?.method).toBe("POST");
    expect(JSON.parse(String(post!.init!.body))).toEqual({
      provider: "trae", model: "glm-5.2",
    });
  });

  it("「立即探活」无法判定时明确说未做禁用（不写成探活失败）", async () => {
    stubProbePage(undefined, (url) =>
      url.includes("/probes/probe")
        ? new Response(JSON.stringify({
            provider: "codebuddy", model: "seed-2.1",
            state: "inconclusive", error: "上游未返回任何内容",
          }), { status: 200, headers: { "Content-Type": "application/json" } })
        : undefined);
    renderPage(<ModelCatalogPage />);
    await settle();

    fireEvent.click(screen.getByTestId("probe-now-codebuddy-seed-2.1"));
    await waitFor(() => expect(screen.getByTestId("model-probe-toast"))
      .toHaveTextContent("无法判定（不构成下线证据，未做任何禁用）"));
  });

  it("「立即探活」上游确认不可用时提示结论", async () => {
    stubProbePage(undefined, (url) =>
      url.includes("/probes/probe")
        ? new Response(JSON.stringify({
            provider: "trae", model: "glm-5.2",
            state: "failed", error: "HTTP 404: model not found",
          }), { status: 200, headers: { "Content-Type": "application/json" } })
        : undefined);
    renderPage(<ModelCatalogPage />);
    await settle();

    fireEvent.click(screen.getByTestId("probe-now-trae-glm-5.2"));
    await waitFor(() => expect(screen.getByTestId("model-probe-toast"))
      .toHaveTextContent("确认不可用：HTTP 404: model not found"));
  });

  it("探活请求本身失败时给出错误提示", async () => {
    stubProbePage(undefined, (url) =>
      url.includes("/probes/probe")
        ? new Response(JSON.stringify({
            error: { code: "forbidden", message: "operator only" },
          }), { status: 403, headers: { "Content-Type": "application/json" } })
        : undefined);
    renderPage(<ModelCatalogPage />);
    await settle();

    fireEvent.click(screen.getByTestId("probe-now-trae-glm-5.2"));
    await waitFor(() => expect(screen.getByTestId("model-probe-toast"))
      .toHaveTextContent("探活失败：operator only"));
  });

  it("「人工接管」写 manual=on，已接管的行写 manual=off（交还）", async () => {
    const calls = stubProbePage(undefined, (url) =>
      url.includes("/probes/manual")
        ? new Response(JSON.stringify({
            provider: "trae", model: "glm-5.2", manual: true,
          }), { status: 200, headers: { "Content-Type": "application/json" } })
        : undefined);
    renderPage(<ModelCatalogPage />);
    await settle();

    fireEvent.click(screen.getByTestId("probe-manual-trae-glm-5.2"));
    await waitFor(() => expect(screen.getByTestId("model-probe-toast"))
      .toHaveTextContent("已人工接管：自动探活跳过它"));
    expect(JSON.parse(String(
      calls.find((call) => call.url.includes("/probes/manual"))!.init!.body,
    ))).toEqual({ provider: "trae", model: "glm-5.2", manual: "on" });

    fireEvent.click(screen.getByTestId("probe-manual-codebuddy-seed-2.1"));
    await waitFor(() => expect(screen.getByTestId("model-probe-toast"))
      .toHaveTextContent("已交还自动探活"));
    expect(JSON.parse(String(
      calls.filter((call) => call.url.includes("/probes/manual")).at(-1)!.init!.body,
    ))).toEqual({ provider: "codebuddy", model: "seed-2.1", manual: "off" });
  });

  it("切换接管失败时给出错误提示", async () => {
    stubProbePage(undefined, (url) =>
      url.includes("/probes/manual")
        ? new Response(JSON.stringify({
            error: { code: "forbidden", message: "operator only" },
          }), { status: 403, headers: { "Content-Type": "application/json" } })
        : undefined);
    renderPage(<ModelCatalogPage />);
    await settle();

    fireEvent.click(screen.getByTestId("probe-manual-trae-glm-5.2"));
    await waitFor(() => expect(screen.getByTestId("model-probe-toast"))
      .toHaveTextContent("切换接管失败：operator only"));
  });
});
