import { screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PlaygroundPage } from "../PlaygroundPage";
import { jsonResponse, mockFetch, renderPage, userEvent } from "./helpers";

const MODELS = {
  object: "list",
  data: [
    { id: "glm-5.2", object: "model", owned_by: "coding2api", providers: ["codebuddy", "trae"] },
  ],
};

/** 构造一个 OpenAI 风格 SSE 流。 */
function sseStream(frames: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const frame of frames) controller.enqueue(encoder.encode(frame));
      controller.close();
    },
  });
}

describe("PlaygroundPage", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("渲染基础表单", () => {
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    expect(screen.getByTestId("playground-key")).toBeInTheDocument();
    expect(screen.getByTestId("load-models")).toBeInTheDocument();
    expect(screen.getByTestId("playground-prompt")).toBeInTheDocument();
  });

  it("载入模型列表并在下拉中展示可选上游", async () => {
    mockFetch({ "/v1/models": MODELS });
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });

    await userEvent.type(screen.getByTestId("playground-key"), "sk-test");
    await userEvent.click(screen.getByTestId("load-models"));

    const select = await screen.findByTestId("model-select");
    expect(select).toHaveTextContent("glm-5.2");
    expect(select).toHaveTextContent("codebuddy / trae");
  });

  it("模型加载失败时提示 Key 无效", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ error: { message: "nope" } }, 401)));
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });

    await userEvent.click(screen.getByTestId("load-models"));
    expect(await screen.findByTestId("playground-error")).toHaveTextContent("模型列表加载失败");
  });

  it("可通过下拉强制指定上游，生成 model@provider", async () => {
    mockFetch({ "/v1/models": MODELS });
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await userEvent.type(screen.getByTestId("playground-key"), "sk-test");
    await userEvent.click(screen.getByTestId("load-models"));
    await screen.findByTestId("model-select");

    await userEvent.selectOptions(screen.getByTestId("provider-pin"), "trae");
    expect(screen.getByTestId("model-select")).toHaveValue("glm-5.2@trae");

    await userEvent.selectOptions(screen.getByTestId("provider-pin"), "");
    expect(screen.getByTestId("model-select")).toHaveValue("glm-5.2");
  });

  it("非流式请求展示回答与 usage", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/v1/models") return jsonResponse(MODELS);
        return jsonResponse({
          choices: [{ message: { content: "你好，世界", reasoning_content: "想一下" } }],
          usage: { prompt_tokens: 3, completion_tokens: 5, credit: null },
        });
      }),
    );

    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await userEvent.type(screen.getByTestId("playground-key"), "sk-test");
    await userEvent.click(screen.getByTestId("load-models"));
    await screen.findByTestId("model-select");

    await userEvent.click(screen.getByTestId("stream-toggle")); // 关闭流式
    await userEvent.type(screen.getByTestId("playground-prompt"), "你好");
    await userEvent.click(screen.getByTestId("send-request"));

    expect(await screen.findByTestId("playground-answer")).toHaveTextContent("你好，世界");
    expect(screen.getByTestId("playground-reasoning")).toHaveTextContent("想一下");
    const usage = screen.getByTestId("playground-usage");
    expect(usage).toHaveTextContent("credit");
    expect(usage).toHaveTextContent("经常不返回");
  });

  it("非流式错误响应展示 message", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/v1/models") return jsonResponse(MODELS);
        return jsonResponse({ error: { message: "上游不可用" } }, 503);
      }),
    );

    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await userEvent.type(screen.getByTestId("playground-key"), "sk-test");
    await userEvent.click(screen.getByTestId("load-models"));
    await screen.findByTestId("model-select");
    await userEvent.click(screen.getByTestId("stream-toggle"));
    await userEvent.type(screen.getByTestId("playground-prompt"), "hi");
    await userEvent.click(screen.getByTestId("send-request"));

    expect(await screen.findByTestId("playground-error")).toHaveTextContent("上游不可用");
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
        if (url === "/v1/models") return jsonResponse(MODELS);
        return new Response(sseStream(frames), {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        });
      }),
    );

    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await userEvent.type(screen.getByTestId("playground-key"), "sk-test");
    await userEvent.click(screen.getByTestId("load-models"));
    await screen.findByTestId("model-select");
    await userEvent.type(screen.getByTestId("playground-prompt"), "你好");
    await userEvent.click(screen.getByTestId("send-request"));

    await waitFor(() =>
      expect(screen.getByTestId("playground-answer")).toHaveTextContent("你好，世界"),
    );
    expect(screen.getByTestId("playground-reasoning")).toHaveTextContent("思考中");
  });

  it("流式 error 帧展示错误并停止", async () => {
    const frames = [
      'data: {"error":{"message":"凭证全部不可用"}}\n\n',
      "data: [DONE]\n\n",
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/v1/models") return jsonResponse(MODELS);
        return new Response(sseStream(frames), { status: 200 });
      }),
    );

    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await userEvent.type(screen.getByTestId("playground-key"), "sk-test");
    await userEvent.click(screen.getByTestId("load-models"));
    await screen.findByTestId("model-select");
    await userEvent.type(screen.getByTestId("playground-prompt"), "hi");
    await userEvent.click(screen.getByTestId("send-request"));

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
        if (url === "/v1/models") return jsonResponse(MODELS);
        return new Response(sseStream(frames), { status: 200 });
      }),
    );

    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    await userEvent.type(screen.getByTestId("playground-key"), "sk-test");
    await userEvent.click(screen.getByTestId("load-models"));
    await screen.findByTestId("model-select");
    await userEvent.type(screen.getByTestId("playground-prompt"), "hi");
    await userEvent.click(screen.getByTestId("send-request"));

    await waitFor(() => expect(screen.getByTestId("playground-answer")).toHaveTextContent("ok"));
  });

  it("初始提示没有响应", () => {
    renderPage(<PlaygroundPage />, { username: "root", is_admin: true });
    expect(screen.getByText("还没有响应")).toBeInTheDocument();
  });
});
