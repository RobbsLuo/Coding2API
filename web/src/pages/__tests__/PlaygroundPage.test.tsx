import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PlaygroundPage } from "../PlaygroundPage";
import { jsonResponse, mockFetch, renderPage, userEvent } from "@/test-utils";

const MODELS = {
  object: "list",
  data: [
    {
      id: "glm-5.2",
      object: "model",
      owned_by: "Coding2API",
      providers: ["codebuddy", "trae"],
    },
  ],
};

function sseStream(frames: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const frame of frames) controller.enqueue(encoder.encode(frame));
      controller.close();
    },
  });
}

/** 等模型数据真正到达（列表里出现第一行模型），而不是只等容器出现。 */
async function waitForModelLoaded() {
  await waitFor(
    () => {
      if (!document.querySelector('[data-testid^="model-option-"]')) {
        throw new Error("模型尚未载入");
      }
    },
    { timeout: 3000 },
  );
}

async function fillPromptAndSend(prompt = "hi") {
  await userEvent.type(screen.getByTestId("playground-prompt"), prompt);
  await userEvent.click(screen.getByTestId("send-request"));
}

describe("PlaygroundPage（会话鉴权，无需 API Key）", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("渲染表单，且不再要求用户填写 API Key", () => {
    mockFetch({ "/api/playground/models": MODELS });
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    expect(screen.getByTestId("playground-page")).toBeInTheDocument();
    expect(screen.queryByTestId("playground-key")).not.toBeInTheDocument();
  });

  it("自定义别名条目在选择器里带「自定义别名」徽章（当前选择条同样标）", async () => {
    mockFetch({ "/api/playground/models": {
      object: "list",
      data: [
        {
          id: "team-fast", object: "model", owned_by: "Coding2API",
          providers: ["trae"], name: "team-fast",
          custom_alias: true, alias_target: "glm-5.2",
        },
      ],
    } });
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();

    const option = screen.getByTestId("model-option-team-fast@trae");
    expect(within(option).getByTestId("model-alias-badge")).toHaveTextContent("自定义别名");
    expect(within(screen.getByTestId("model-current"))
      .getByTestId("model-current-alias")).toHaveTextContent("自定义别名");
  });

  it("默认选中倍率最小的模型；选中后展示上限与能力，双渠道倍率在选择器里按渠道显示", async () => {
    const META_MODELS = {
      object: "list",
      data: [
        {
          id: "glm-5.2", object: "model", owned_by: "Coding2API",
          providers: ["codebuddy", "trae"],
          credit_rate: 0.29, max_input_tokens: 200000,
          supports_images: false, supports_tool_call: true,
          by_provider: { codebuddy: { credit_rate: 0.29 }, trae: { credit_rate: 0.17 } },
        },
        {
          id: "DeepSeek-V4-Flash-Official", object: "model", owned_by: "Coding2API",
          providers: ["trae"],
          credit_rate: 0.08, max_input_tokens: 256000,
        },
      ],
    };
    mockFetch({ "/api/playground/models": META_MODELS });
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();

    // 默认选中倍率最小的模型（DeepSeek 单渠道 TRAE x0.08 < glm-5.2 的 0.17）
    expect(screen.getByTestId("model-option-DeepSeek-V4-Flash-Official@trae"))
      .toHaveAttribute("aria-selected", "true");
    // 「当前选择」条展示渠道与倍率（渠道/倍率不再挤在能力元数据卡里）
    const current = screen.getByTestId("model-current");
    expect(current).toHaveTextContent("DeepSeek-V4-Flash-Official");
    expect(current).toHaveTextContent("x0.08");
    expect(screen.getByTestId("model-meta")).toHaveTextContent("256,000");

    // 切到双渠道模型：当前选择条与列表行都按渠道分别标注倍率
    await userEvent.click(screen.getByTestId("model-option-glm-5.2"));
    const current2 = screen.getByTestId("model-current");
    expect(current2).toHaveTextContent("CodeBuddy");
    expect(current2).toHaveTextContent("TRAE");
    expect(current2).toHaveTextContent("x0.29");
    expect(current2).toHaveTextContent("x0.17");
    expect(current2).toHaveTextContent("自动路由");
    const meta2 = screen.getByTestId("model-meta");
    expect(meta2).toHaveTextContent("200,000");
    expect(meta2).toHaveTextContent("图片");
    expect(meta2.querySelector("svg.lucide-check")).toBeInTheDocument();
    expect(meta2.querySelector("svg.lucide-x")).toBeInTheDocument();
    expect(meta2).not.toHaveTextContent("✓");

    // 切到单渠道模型：倍率不按渠道拆分，显示合并值
    await userEvent.click(screen.getByTestId("model-option-DeepSeek-V4-Flash-Official@trae"));
    expect(screen.getByTestId("model-current")).toHaveTextContent("x0.08");
    expect(screen.getByTestId("model-current")).not.toHaveTextContent("x0.17");
  });

  it("能力分（Artificial Analysis 指数）在选择器行与元数据卡展示", async () => {
    const RATED_MODELS = {
      object: "list",
      data: [
        {
          id: "glm-5.3", object: "model", owned_by: "Coding2API",
          providers: ["codebuddy", "trae"],
          benchmarks: {
            intelligence_index: 44.8, coding_index: 74.8, agentic_index: 53.1,
            source: "openrouter", source_model: "z-ai/glm-5.3",
          },
        },
        {
          id: "kimi-k3", object: "model", owned_by: "Coding2API",
          providers: ["trae"],
          // 上游只给了综合分：另两项不渲染，也不显示 null
          benchmarks: { intelligence_index: 43.6, source: "openrouter" },
        },
        { id: "no-score", object: "model", owned_by: "Coding2API", providers: ["trae"] },
      ],
    };
    mockFetch({ "/api/playground/models": RATED_MODELS });
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();

    // 默认选中第一条（无倍率数据时回退列表第一个）：三项分数 + 来源说明
    const card = screen.getByTestId("playground-benchmarks");
    expect(card).toHaveTextContent("能力分");
    expect(card).toHaveTextContent("44.8");
    expect(card).toHaveTextContent("74.8");
    expect(card).toHaveTextContent("53.1");
    // 必须标明是第三方成绩，不是本服务实测
    expect(card).toHaveTextContent("非本服务实测");

    // 行内徽章只显示综合分，tooltip 带三项与来源
    const row = screen.getByTestId("model-option-glm-5.3");
    const badge = within(row).getByTestId("model-benchmark");
    expect(badge).toHaveTextContent("智 44.8");
    expect(badge.getAttribute("title")).toContain("编程 74.8");
    expect(badge.getAttribute("title")).toContain("z-ai/glm-5.3");
    expect(badge.getAttribute("title")).toContain("非本服务实测");

    // 只有综合分的模型：徽章照显，卡里只列那一项
    await userEvent.click(screen.getByTestId("model-option-kimi-k3@trae"));
    expect(screen.getByTestId("playground-benchmarks")).toHaveTextContent("43.6");
    expect(screen.getByTestId("playground-benchmarks")).not.toHaveTextContent("74.8");

    // 无分数的模型：整卡不渲染，行里也没有徽章
    await userEvent.click(screen.getByTestId("model-option-no-score@trae"));
    expect(screen.queryByTestId("playground-benchmarks")).not.toBeInTheDocument();
    expect(within(screen.getByTestId("model-option-no-score@trae"))
      .queryByTestId("model-benchmark")).not.toBeInTheDocument();
  });

  it("自动载入模型列表并展示可选渠道，请求走会话端点", async () => {
    const spy = mockFetch({ "/api/playground/models": MODELS });
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();

    // 双渠道模型出现在「自动调度」分组，且默认选中（无倍率数据回退第一个）
    expect(screen.getByTestId("model-option-glm-5.2")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("model-section-auto")).toHaveTextContent("多渠道（自动调度）");
    expect(spy.mock.calls.some(([url]) => String(url).includes("/api/playground/models"))).toBe(true);
    expect(spy.mock.calls.some(([url]) => String(url).includes("/v1/models"))).toBe(false);
  });

  it("模型列表每行展示全部渠道徽章与各自倍率", async () => {
    const RATED_MODELS = {
      object: "list",
      data: [
        {
          id: "glm-5.2", object: "model", owned_by: "Coding2API",
          providers: ["codebuddy", "trae"], credit_rate: 0.29,
          by_provider: { codebuddy: { credit_rate: 0.29 }, trae: { credit_rate: 0.17 } },
        },
        {
          id: "kimi-k3", object: "model", owned_by: "Coding2API",
          providers: ["trae"], credit_rate: 0.08,
        },
        { id: "no-rate", object: "model", owned_by: "Coding2API", providers: ["trae"] },
      ],
    };
    mockFetch({ "/api/playground/models": RATED_MODELS });
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();

    const dual = screen.getByTestId("model-option-glm-5.2");
    expect(dual).toHaveTextContent("CodeBuddy");
    expect(dual).toHaveTextContent("x0.29");
    expect(dual).toHaveTextContent("TRAE");
    expect(dual).toHaveTextContent("x0.17");
    expect(dual.querySelector('[data-testid="channel-pill-codebuddy"]')).toBeInTheDocument();
    expect(dual.querySelector('[data-testid="channel-pill-trae"]')).toBeInTheDocument();
    // 单渠道：徽章带渠道名与合并倍率
    const single = screen.getByTestId("model-option-kimi-k3@trae");
    expect(single).toHaveTextContent("TRAE");
    expect(single).toHaveTextContent("x0.08");
    // 无倍率数据：只显示渠道徽章，不显示倍率
    const noRate = screen.getByTestId("model-option-no-rate@trae");
    expect(noRate).toHaveTextContent("TRAE");
    // 无倍率数据：渠道徽章里没有倍率块（「免」/「x」数字）
    expect(noRate.querySelector('[data-testid="channel-pill-trae"]')).toBeInTheDocument();
    expect(noRate.textContent).not.toMatch(/x\d|免/);
  });

  it("多渠道徽章不挤压模型名：名称保底宽度、徽章在右侧折行", async () => {
    const FOUR = {
      object: "list",
      data: [
        {
          id: "deepseek-v4.1-flash", object: "model", owned_by: "Coding2API",
          providers: ["codebuddy", "trae", "qoder", "codearts"],
        },
      ],
    };
    mockFetch({ "/api/playground/models": FOUR });
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();

    const row = screen.getByTestId("model-option-deepseek-v4.1-flash");
    // 整行不换行：徽章永远留在名称右侧，不整组掉到名称下方
    expect(row).not.toHaveClass("flex-wrap");
    // 名称保底宽度（min-w-40），四渠道也挤不成省略号
    const nameBox = row.querySelector(".min-w-40");
    expect(nameBox).toHaveClass("flex-1");
    expect(nameBox).toHaveTextContent("deepseek-v4.1-flash");
    // 徽章组自身折行且在右侧区域内贴右（justify-end），不会跑到左边
    const pillBox = row.lastElementChild;
    expect(pillBox).toHaveClass("flex-wrap", "justify-end");
    for (const provider of ["codebuddy", "trae", "qoder", "codearts"]) {
      expect(row.querySelector(`[data-testid="channel-pill-${provider}"]`)).toBeInTheDocument();
    }
  });

  it("多渠道缺细分倍率时只标注有倍率的渠道，不裸显合并值", async () => {
    const EDGE_MODELS = {
      object: "list",
      data: [
        {
          id: "one-sided", object: "model", owned_by: "Coding2API",
          providers: ["codebuddy", "trae"], credit_rate: 0.29,
          by_provider: { codebuddy: { credit_rate: 0.29 } },
        },
      ],
    };
    mockFetch({ "/api/playground/models": EDGE_MODELS });
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();

    const row = screen.getByTestId("model-option-one-sided");
    expect(row).toHaveTextContent("CodeBuddy");
    expect(row).toHaveTextContent("x0.29");
    expect(row).toHaveTextContent("TRAE");
    // TRAE 没有细分倍率：不拿合并值冒充，因此整行只有一个 x
    expect(row.textContent?.match(/x/g)).toHaveLength(1);
  });

  it("渠道筛选与搜索收敛列表", async () => {
    const mixed = {
      object: "list",
      data: [
        { id: "glm-5.2", object: "model", owned_by: "x", providers: ["codebuddy", "trae"] },
        { id: "kimi-k3", object: "model", owned_by: "x", providers: ["trae"] },
      ],
    };
    mockFetch({ "/api/playground/models": mixed });
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();

    // 「多渠道」筛选只留多渠道模型
    await userEvent.click(screen.getByTestId("model-filter-auto"));
    expect(screen.getByTestId("model-option-glm-5.2")).toBeInTheDocument();
    expect(screen.queryByTestId("model-option-kimi-k3@trae")).not.toBeInTheDocument();

    // 恢复全部后按关键词搜索（大小写不敏感）
    await userEvent.click(screen.getByTestId("model-filter-all"));
    await userEvent.type(screen.getByTestId("model-search"), "KIMI");
    expect(screen.getByTestId("model-option-kimi-k3@trae")).toBeInTheDocument();
    expect(screen.queryByTestId("model-option-glm-5.2")).not.toBeInTheDocument();

    // 无匹配时给出空态
    await userEvent.clear(screen.getByTestId("model-search"));
    await userEvent.type(screen.getByTestId("model-search"), "zzz");
    expect(screen.getByText("没有匹配的模型")).toBeInTheDocument();
  });

  it("渠道徽章只渲染渠道名与倍率，福标已随渠道重构移除", async () => {
    const CHANNEL_MODELS = {
      object: "list",
      data: [
        {
          id: "deepseek-v4.1-flash", object: "model", owned_by: "Coding2API",
          providers: ["codearts", "trae"],
          by_provider: {
            codearts: { raw_id: "deepseek-v4.1-flash" },
            trae: { raw_id: "deepseek-v4.1-flash", credit_rate: 0.08 },
          },
        },
        {
          id: "codearts-only", object: "model", owned_by: "Coding2API",
          providers: ["codearts"],
        },
      ],
    };
    mockFetch({ "/api/playground/models": CHANNEL_MODELS });
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();

    // 多渠道：两侧渠道徽章只渲染渠道名（trae 侧带倍率），不再有「福」标
    const dual = screen.getByTestId("model-option-deepseek-v4.1-flash");
    expect(within(dual).getByTestId("channel-pill-codearts")).toHaveTextContent("CodeArts");
    expect(within(dual).getByTestId("channel-pill-trae")).toHaveTextContent("TRAE");
    expect(within(dual).getByTestId("channel-pill-trae")).toHaveTextContent("x0.08");
    expect(dual).not.toHaveTextContent("福");
    expect(dual.querySelector('[data-testid^="channel-benefit-"]')).not.toBeInTheDocument();

    // 单渠道 CodeArts：同样只有渠道名与（可选的）倍率
    const single = screen.getByTestId("model-option-codearts-only@codearts");
    expect(within(single).getByTestId("channel-pill-codearts")).toHaveTextContent("CodeArts");
    expect(single).not.toHaveTextContent("福");
    expect(single.querySelector('[data-testid^="channel-benefit-"]')).not.toBeInTheDocument();
  });

  it("免费模型（x0）的倍率块显示「免」，单渠道当前选择条不打自动路由标", async () => {
    const FREE_MODELS = {
      object: "list",
      data: [
        {
          id: "kilo-auto/free", object: "model", owned_by: "Coding2API",
          providers: ["kilo"], credit_rate: 0,
        },
      ],
    };
    mockFetch({ "/api/playground/models": FREE_MODELS });
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();

    const current = screen.getByTestId("model-current");
    expect(current).toHaveTextContent("kilo-auto/free");
    expect(within(current).getByTestId("channel-pill-kilo")).toHaveTextContent("免");
    // 单渠道模型不显示「自动路由」标
    expect(current).not.toHaveTextContent("自动路由");
  });

  it("模型加载失败时给出提示", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ error: { message: "nope" } }, 401)));
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    expect(await screen.findByTestId("playground-error")).toHaveTextContent("模型列表加载失败");
  });

  it("强制指定渠道只列该模型可用渠道，生成 model@provider 并可恢复自动路由", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push({ url, init });
        if (url.includes("/api/playground/models")) return jsonResponse(MODELS);
        return jsonResponse({ choices: [{ message: { content: "ok" } }] });
      }),
    );
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();

    // 多渠道模型：两个渠道 chip + 自动路由
    expect(screen.getByTestId("provider-pin-codebuddy")).toBeInTheDocument();
    expect(screen.getByTestId("provider-pin-trae")).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("provider-pin-trae"));
    expect(screen.getByTestId("provider-pin-trae")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("model-current")).toHaveTextContent("已强制 TRAE");

    await fillPromptAndSend("hi");
    const chat = calls.find((item) => item.url.includes("/api/playground/chat/completions"));
    expect(JSON.parse(chat!.init!.body as string).model).toBe("glm-5.2@trae");

    await userEvent.click(screen.getByTestId("provider-pin-auto"));
    expect(screen.getByTestId("provider-pin-auto")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("model-current")).toHaveTextContent("自动路由");
  });

  it("单渠道模型不显示强制指定渠道控件", async () => {
    const single = {
      object: "list",
      data: [
        { id: "kimi-k3", object: "model", owned_by: "x", providers: ["trae"] },
      ],
    };
    mockFetch({ "/api/playground/models": single });
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();
    expect(screen.queryByTestId("provider-pin")).not.toBeInTheDocument();
  });

  it("非流式请求展示回答与 usage，请求体与用量归属正确", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push({ url, init });
        if (url.includes("/api/playground/models")) return jsonResponse(MODELS);
        return jsonResponse({
          choices: [{ message: { content: "你好，世界", reasoning_content: "想一下" } }],
          usage: { prompt_tokens: 3, completion_tokens: 5, credit: null },
        });
      }),
    );

    renderPage(<PlaygroundPage />, { username: "alice", is_admin: false });
    await waitForModelLoaded();
    await userEvent.click(screen.getByTestId("stream-toggle"));
    await fillPromptAndSend("你好");

    expect(await screen.findByTestId("playground-answer")).toHaveTextContent("你好，世界");
    expect(screen.getByTestId("playground-reasoning")).toHaveTextContent("想一下");
    const usage = screen.getByTestId("playground-usage");
    expect(usage).toHaveTextContent("credit");
    expect(usage).toHaveTextContent("经常不返回");

    const chat = calls.find((item) => item.url.includes("/api/playground/chat/completions"));
    expect(chat).toBeDefined();
    expect(chat!.init?.method).toBe("POST");
    expect(JSON.parse(chat!.init!.body as string)).toEqual({
      model: "glm-5.2",
      messages: [{ role: "user", content: "你好" }],
      stream: false,
    });
  });

  it("非流式错误响应展示 message", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/playground/models")) return jsonResponse(MODELS);
        return jsonResponse({ error: { message: "渠道不可用" } }, 503);
      }),
    );

    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();
    await userEvent.click(screen.getByTestId("stream-toggle"));
    await fillPromptAndSend("hi");
    expect(await screen.findByTestId("playground-error")).toHaveTextContent("渠道不可用");
  });

  it("会话过期时跳回登录页", async () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { href: "/", assign });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/playground/models")) return jsonResponse(MODELS);
        return jsonResponse({ error: { code: "unauthorized" } }, 401);
      }),
    );

    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();
    await fillPromptAndSend("hi");
    await waitFor(() => expect(window.location.href).toBe("/login"));
  });

  it("流式请求按 chunk 累积 content 与 reasoning，遇 [DONE] 结束", async () => {
    const frames = [
      'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}\n\n',
      'data: {"choices":[{"delta":{"reasoning_content":"思考中"}}]}\n\n',
      'data: {"choices":[{"delta":{"content":"你好"}}]}\n\n',
      'data: {"choices":[{"delta":{"content":"，世界"}}]}\n\n',
      "data: [DONE]\n\n",
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/playground/models")) return jsonResponse(MODELS);
        return new Response(sseStream(frames), {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        });
      }),
    );

    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();
    await fillPromptAndSend("你好");

    await waitFor(() =>
      expect(screen.getByTestId("playground-answer")).toHaveTextContent("你好，世界"),
    );
    expect(screen.getByTestId("playground-reasoning")).toHaveTextContent("思考中");
  });

  it("流式 error 帧展示错误并停止", async () => {
    const frames = ['data: {"error":{"message":"凭证全部不可用"}}\n\n', "data: [DONE]\n\n"];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/playground/models")) return jsonResponse(MODELS);
        return new Response(sseStream(frames), { status: 200 });
      }),
    );

    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();
    await fillPromptAndSend("hi");
    expect(await screen.findByTestId("playground-error")).toHaveTextContent("凭证全部不可用");
  });

  it("流式忽略非 JSON 帧（心跳）", async () => {
    const frames = [
      ": keepalive\n\n",
      "data: not-json\n\n",
      'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
      "data: [DONE]\n\n",
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/playground/models")) return jsonResponse(MODELS);
        return new Response(sseStream(frames), { status: 200 });
      }),
    );

    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();
    await fillPromptAndSend("hi");
    await waitFor(() => expect(screen.getByTestId("playground-answer")).toHaveTextContent("ok"));
  });

  it("初始提示说明无需 API Key，用量归属当前用户", () => {
    mockFetch({ "/api/playground/models": MODELS });
    renderPage(<PlaygroundPage />, { username: "alice", is_admin: false });
    expect(screen.getByText(/无需 API Key/)).toBeInTheDocument();
    expect(screen.getByText(/计入 alice/)).toBeInTheDocument();
  });

  it("仅单一渠道可用的模型按分组展示（避免淹没在长列表里）", async () => {
    const mixed = {
      object: "list",
      data: [
        { id: "glm-5.2", object: "model", owned_by: "x",
          providers: ["codebuddy", "trae"] },
        { id: "deepseek-v4-pro", object: "model", owned_by: "x",
          providers: ["codebuddy"] },
        { id: "kimi-k3", object: "model", owned_by: "x", providers: ["trae"] },
      ],
    };
    mockFetch({ "/api/playground/models": mixed });
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();

    const auto = screen.getByTestId("model-section-auto");
    expect(auto).toHaveTextContent("多渠道（自动调度）");
    expect(screen.getByTestId("model-section-codebuddy")).toHaveTextContent("仅 CodeBuddy");
    expect(screen.getByTestId("model-section-trae")).toHaveTextContent("仅 TRAE");
    // 多渠道模型保留裸值（走自动路由），单渠道带 @provider
    expect(screen.getByTestId("model-option-glm-5.2")).toBeInTheDocument();
    expect(screen.getByTestId("model-option-deepseek-v4-pro@codebuddy")).toBeInTheDocument();
    expect(screen.getByTestId("model-option-kimi-k3@trae")).toBeInTheDocument();
  });

  it("单渠道分组按 CB → TR → 其余排序，不随模型列表首次出现顺序", async () => {
    const mixed = {
      object: "list",
      data: [
        // zen 先出现，但展示分组应排到最后
        { id: "aaa-zen", object: "model", owned_by: "x", providers: ["zen"] },
        { id: "kimi-k3", object: "model", owned_by: "x", providers: ["trae"] },
        { id: "deepseek-v4-pro", object: "model", owned_by: "x",
          providers: ["codebuddy"] },
      ],
    };
    mockFetch({ "/api/playground/models": mixed });
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();

    const labels = [...document.querySelectorAll('[data-testid^="model-section-"]')]
      .map((node) => node.textContent);
    expect(labels).toEqual(["仅 CodeBuddy", "仅 TRAE", "仅 OpenCode Zen"]);
  });

  it("没有可用模型时给出空态", async () => {
    mockFetch({ "/api/playground/models": { object: "list", data: [] } });
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await screen.findByTestId("model-picker");
    expect(screen.getByText("没有匹配的模型")).toBeInTheDocument();
  });

  it("展示上游人类可读名（Qoder 代号 id 显示为 Qwen3.8-Max）", async () => {
    const QODER = {
      object: "list",
      data: [
        { id: "qmodel_38max", object: "model", owned_by: "Coding2API",
          name: "Qwen3.8-Max", providers: ["qoder"] },
        { id: "dmodel", object: "model", owned_by: "Coding2API",
          providers: ["qoder"] },   // 无 name：回退显示 id
      ],
    };
    mockFetch({ "/api/playground/models": QODER });
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();

    // 有 name：主名显示可读名，副行保留内部 id（便于 model@provider 直连）
    const row = screen.getByTestId("model-option-qmodel_38max@qoder");
    expect(row).toHaveTextContent("Qwen3.8-Max");
    expect(row).toHaveTextContent("qmodel_38max");
    // 无 name：直接用 id 当主名
    expect(screen.getByTestId("model-option-dmodel@qoder")).toHaveTextContent("dmodel");

    // 搜索按可读名也能命中
    await userEvent.type(screen.getByTestId("model-search"), "qwen");
    expect(screen.getByTestId("model-option-qmodel_38max@qoder")).toBeInTheDocument();
    expect(screen.queryByTestId("model-option-dmodel@qoder")).not.toBeInTheDocument();
  });

  const REASONING_MODELS = {
    object: "list",
    data: [
      { id: "thinker", object: "model", owned_by: "Coding2API", providers: ["codebuddy"],
        supports_reasoning: true, default_effort: "medium", max_output_tokens: 8192 },
      { id: "plain", object: "model", owned_by: "Coding2API", providers: ["codebuddy"] },
    ],
  };

  /** 记录 chat 请求体的 fetch mock（模型列表按需返回）。 */
  function spyChat(models: unknown) {
    const calls: { url: string; init?: RequestInit }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push({ url, init });
        if (url.includes("/api/playground/models")) return jsonResponse(models);
        return jsonResponse({
          choices: [{ message: { content: "ok" } }],
          usage: { prompt_tokens: 1, completion_tokens: 1 },
        });
      }),
    );
    return calls;
  }

  function chatBody(calls: { url: string; init?: RequestInit }[]) {
    // 取**最后一次** chat 调用：切换模型后重发时，`find` 会命中最先那次
    const chat = calls.filter((item) => item.url.includes("/api/playground/chat/completions")).at(-1);
    return JSON.parse((chat!.init as RequestInit).body as string) as Record<string, unknown>;
  }

  it("高级参数留空时请求体不含这三个键（交给渠道默认）", async () => {
    const calls = spyChat(MODELS);
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();
    await fillPromptAndSend("hi");

    const body = chatBody(calls);
    expect(body).not.toHaveProperty("temperature");
    expect(body).not.toHaveProperty("max_tokens");
    expect(body).not.toHaveProperty("reasoning_effort");
  });

  it("填写高级参数后进入请求体；未标推理能力的模型不渲染档位控件", async () => {
    const calls = spyChat(REASONING_MODELS);
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();

    // 默认选中第一条（有推理元数据）：控件在，默认值为「不发送」，并提示上游默认档位
    expect(screen.getByTestId("param-effort")).toHaveValue("");
    expect(screen.getByTestId("playground-advanced")).toHaveTextContent("该模型默认档位 medium");
    expect(screen.getByTestId("playground-advanced")).toHaveTextContent("该模型上限 8,192");

    await userEvent.type(screen.getByTestId("param-temperature"), "0.7");
    await userEvent.type(screen.getByTestId("param-max-tokens"), "256");
    await userEvent.selectOptions(screen.getByTestId("param-effort"), "high");
    await fillPromptAndSend("hi");

    expect(chatBody(calls)).toMatchObject({
      temperature: 0.7, max_tokens: 256, reasoning_effort: "high",
    });

    // 切到无推理元数据的模型：档位控件消失，且请求体不再带 reasoning_effort
    await userEvent.click(screen.getByTestId("model-option-plain@codebuddy"));
    expect(screen.queryByTestId("param-effort")).not.toBeInTheDocument();
    await fillPromptAndSend("hi");
    const body = chatBody(calls);
    expect(body).not.toHaveProperty("reasoning_effort");
    expect(body).toHaveProperty("temperature", 0.7);
  });

  it("切换模型清空推理档位，不把上一个模型的档位带给新模型", async () => {
    spyChat(REASONING_MODELS);
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();

    await userEvent.selectOptions(screen.getByTestId("param-effort"), "high");
    expect(screen.getByTestId("param-effort")).toHaveValue("high");
    await userEvent.click(screen.getByTestId("model-option-plain@codebuddy"));
    await userEvent.click(screen.getByTestId("model-option-thinker@codebuddy"));
    expect(screen.getByTestId("param-effort")).toHaveValue("");
  });

  it("提示词框内 Ctrl/⌘ + Enter 直接发送", async () => {
    const calls = spyChat(MODELS);
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();

    await userEvent.type(screen.getByTestId("playground-prompt"), "hi");
    await userEvent.keyboard("{Control>}{Enter}{/Control}");

    await waitFor(() =>
      expect(calls.some((item) => item.url.includes("/api/playground/chat/completions"))).toBe(true),
    );
  });

  it("多渠道时当前选择条只渲染一份渠道徽章（徽章即渠道按钮）", async () => {
    mockFetch({ "/api/playground/models": MODELS });
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();

    const current = screen.getByTestId("model-current");
    // 徽章不再静态列一遍、渠道按钮再列一遍：每个渠道在「当前选择」条里只出现一次
    for (const provider of ["codebuddy", "trae"]) {
      expect(within(current).getAllByTestId(`channel-pill-${provider}`)).toHaveLength(1);
      // 徽章就挂在渠道按钮内部，点它即强制该渠道
      expect(
        within(within(current).getByTestId(`provider-pin-${provider}`))
          .getByTestId(`channel-pill-${provider}`),
      ).toBeInTheDocument();
    }
  });

  it("请求进行中再次提交被忽略，不重复发请求", async () => {
    let release: () => void = () => {};
    const pending = new Promise<void>((resolve) => {
      release = resolve;
    });
    const calls: { url: string }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        calls.push({ url });
        if (url.includes("/api/playground/models")) return jsonResponse(MODELS);
        await pending;
        return jsonResponse({ choices: [{ message: { content: "ok" } }] });
      }),
    );
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();
    await userEvent.click(screen.getByTestId("stream-toggle"));

    await userEvent.type(screen.getByTestId("playground-prompt"), "hi");
    await userEvent.click(screen.getByTestId("send-request"));

    // 请求在途：⌘/Ctrl+Enter 走 requestSubmit()，真实浏览器里不受发送按钮
    // disabled 约束（jsdom 会拦，所以这里直接派发 submit 事件来覆盖该路径）。
    // 没有 busy 守卫时这里会发出第二个 POST，重复计费。
    const form = screen.getByTestId("playground-prompt").closest("form")!;
    fireEvent.submit(form);

    release();
    await waitFor(() =>
      expect(screen.getByTestId("playground-answer")).toHaveTextContent("ok"),
    );
    expect(calls.filter((item) => item.url.includes("/api/playground/chat/completions")))
      .toHaveLength(1);
  });

  it("非法数值拦住发送并给出提示，不发请求", async () => {
    const calls = spyChat(MODELS);
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();

    await userEvent.type(screen.getByTestId("param-temperature"), "2000");
    await fillPromptAndSend("hi");
    expect(screen.getByTestId("playground-error")).toHaveTextContent("温度需在 0–1000 之间");
    expect(calls.some((item) => item.url.includes("/api/playground/chat/completions"))).toBe(false);
  });
});