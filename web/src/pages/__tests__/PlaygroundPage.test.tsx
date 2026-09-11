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
      owned_by: "coding2api",
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

  it("自动载入模型列表并展示可选上游，请求走会话端点", async () => {
    const spy = mockFetch({ "/api/playground/models": MODELS });
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();

    expect(screen.getByTestId("model-select")).toHaveTextContent("glm-5.2");
    expect(screen.getByTestId("model-select")).toHaveTextContent("codebuddy / trae");
    expect(spy.mock.calls.some(([url]) => String(url).includes("/api/playground/models"))).toBe(true);
    expect(spy.mock.calls.some(([url]) => String(url).includes("/v1/models"))).toBe(false);
  });

  it("模型加载失败时给出提示", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ error: { message: "nope" } }, 401)));
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    expect(await screen.findByTestId("playground-error")).toHaveTextContent("模型列表加载失败");
  });

  it("强制指定上游生成 model@provider，恢复自动路由去掉后缀", async () => {
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
        return jsonResponse({ error: { message: "上游不可用" } }, 503);
      }),
    );

    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await waitForModelLoaded();
    await userEvent.click(screen.getByTestId("stream-toggle"));
    await fillPromptAndSend("hi");
    expect(await screen.findByTestId("playground-error")).toHaveTextContent("上游不可用");
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

  it("没有可用模型时 select 为空", async () => {
    mockFetch({ "/api/playground/models": { object: "list", data: [] } });
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await screen.findByTestId("model-select");
    expect((screen.getByTestId("model-select") as HTMLSelectElement).value).toBe("");
  });
});
