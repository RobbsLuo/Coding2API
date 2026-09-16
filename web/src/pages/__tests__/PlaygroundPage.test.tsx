import { screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PlaygroundPage } from "../PlaygroundPage";
import { jsonResponse, mockFetch, renderPage, userEvent } from "./helpers";

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

/** 等模型数据真正到达（select 有值），而不是只等元素出现。 */
async function waitForModelLoaded() {
  await waitFor(
    () => {
      const select = screen.getByTestId("model-select") as HTMLSelectElement;
      if (!select.value) throw new Error("模型尚未载入");
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

  it("默认选中倍率最小的模型；选中后展示渠道与元数据，双渠道倍率按渠道显示", async () => {
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
    expect((screen.getByTestId("model-select") as HTMLSelectElement).value)
      .toBe("DeepSeek-V4-Flash-Official@trae");
    const meta = screen.getByTestId("model-meta");
    expect(meta).toHaveTextContent("x0.08");
    expect(meta).not.toHaveTextContent("x0.29");

    // 切到双渠道模型：渠道 icon 标注全部显示 + 倍率按渠道分别显示
    await userEvent.selectOptions(screen.getByTestId("model-select"), "glm-5.2");
    const meta2 = screen.getByTestId("model-meta");
    expect(meta2).toHaveTextContent("CodeBuddy");
    expect(meta2).toHaveTextContent("TRAE");
    expect(meta2).toHaveTextContent("x0.29");
    expect(meta2).toHaveTextContent("x0.17");
    expect(meta2).toHaveTextContent("200,000");
    expect(meta2).toHaveTextContent("图片");
    expect(meta2.querySelector("svg.lucide-check")).toBeInTheDocument();
    expect(meta2.querySelector("svg.lucide-x")).toBeInTheDocument();
    expect(meta2).not.toHaveTextContent("✓");

    // 切到单渠道模型：倍率不按渠道拆分，显示合并值
    await userEvent.selectOptions(screen.getByTestId("model-select"),
      "DeepSeek-V4-Flash-Official@trae");
    const meta3 = screen.getByTestId("model-meta");
    expect(meta3).toHaveTextContent("x0.08");
    expect(meta3).not.toHaveTextContent("x0.29");
  });

  it("自动载入模型列表并展示可选渠道，请求走会话端点", async () => {
    const spy = mockFetch({ "/api/playground/models": MODELS });
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();

    const select = screen.getByTestId("model-select");
    // 双渠道模型出现在「自动路由」分组
    expect(select).toHaveTextContent("glm-5.2");
    // 无倍率数据：默认选中回退列表第一个
    expect((select as HTMLSelectElement).value).toBe("glm-5.2");
    const groups = [...select.querySelectorAll("optgroup")].map((g) => g.label);
    expect(groups).toContain("双渠道（自动调度）");
    expect(spy.mock.calls.some(([url]) => String(url).includes("/api/playground/models"))).toBe(true);
    expect(spy.mock.calls.some(([url]) => String(url).includes("/v1/models"))).toBe(false);
  });

  it("模型 option 文本带消耗倍率（双渠道按渠道标注）", async () => {
    const RATED_MODELS = {
      object: "list",
      data: [
        {
          id: "glm-5.2", object: "model", owned_by: "Coding2API",
          providers: ["codebuddy", "trae"], credit_rate: 0.29,
          by_provider: { codebuddy: { credit_rate: 0.29 }, trae: { credit_rate: 0.17 } },
        },
        {
          id: "DeepSeek-V4-Flash-Official", object: "model", owned_by: "Coding2API",
          providers: ["trae"], credit_rate: 0.08,
        },
        { id: "no-rate", object: "model", owned_by: "Coding2API", providers: ["trae"] },
      ],
    };
    mockFetch({ "/api/playground/models": RATED_MODELS });
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();

    const options = [...screen.getByTestId("model-select").querySelectorAll("option")];
    const textOf = (value: string) =>
      options.find((o) => o.value === value)?.textContent ?? "";
    // 双渠道：渠道缩写 + 各自倍率
    expect(textOf("glm-5.2")).toBe("glm-5.2（自动路由 · CB x0.29/TR x0.17）");
    // 单渠道：optgroup 已标渠道，仍带渠道缩写 + 倍率
    expect(textOf("DeepSeek-V4-Flash-Official@trae")).toBe(
      "DeepSeek-V4-Flash-Official · TR x0.08");
    // 无倍率数据：不追加任何标记
    expect(textOf("no-rate@trae")).toBe("no-rate");

    // 双渠道倍率相同：两个渠道都要写出来
    // 双渠道只有一个渠道返回倍率：只标那个渠道，不裸显数字
  });

  it("双渠道倍率相同或缺失时，渠道标注规则", async () => {
    const EDGE_MODELS = {
      object: "list",
      data: [
        {
          id: "same-rate", object: "model", owned_by: "Coding2API",
          providers: ["codebuddy", "trae"], credit_rate: 0.5,
          by_provider: { codebuddy: { credit_rate: 0.5 }, trae: { credit_rate: 0.5 } },
        },
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

    const options = [...screen.getByTestId("model-select").querySelectorAll("option")];
    const textOf = (value: string) =>
      options.find((o) => o.value === value)?.textContent ?? "";
    expect(textOf("same-rate")).toBe("same-rate（自动路由 · CB x0.5/TR x0.5）");
    expect(textOf("one-sided")).toBe("one-sided（自动路由 · CB x0.29）");
  });

  it("模型加载失败时给出提示", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ error: { message: "nope" } }, 401)));
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    expect(await screen.findByTestId("playground-error")).toHaveTextContent("模型列表加载失败");
  });

  it("强制指定渠道生成 model@provider，恢复自动路由去掉后缀", async () => {
    mockFetch({ "/api/playground/models": MODELS });
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();

    await userEvent.selectOptions(screen.getByTestId("provider-pin"), "trae");
    expect(screen.getByTestId("model-select")).toHaveValue("glm-5.2@trae");

    await userEvent.selectOptions(screen.getByTestId("provider-pin"), "");
    expect(screen.getByTestId("model-select")).toHaveValue("glm-5.2");
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

    const select = screen.getByTestId("model-select");
    const groups = [...select.querySelectorAll("optgroup")].map(
      (group) => [group.label, [...group.querySelectorAll("option")].map((o) => o.value)],
    );
    expect(groups).toContainEqual(["仅 CodeBuddy", ["deepseek-v4-pro@codebuddy"]]);
    expect(groups).toContainEqual(["仅 TRAE", ["kimi-k3@trae"]]);
    // 双渠道模型保留原值（不带 @），走自动路由
    const dualGroup = select.querySelector('optgroup[label="双渠道（自动调度）"]');
    expect(dualGroup).not.toBeNull();
    expect(dualGroup!.querySelector("option")?.value).toBe("glm-5.2");
  });

  it("没有可用模型时 select 为空", async () => {
    mockFetch({ "/api/playground/models": { object: "list", data: [] } });
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await screen.findByTestId("model-select");
    expect((screen.getByTestId("model-select") as HTMLSelectElement).value).toBe("");
  });
});
