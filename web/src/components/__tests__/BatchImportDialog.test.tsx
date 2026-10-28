import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../../api/client";
import { BatchImportDialog, parseBatchText } from "../BatchImportDialog";

describe("parseBatchText", () => {
  it("空文本不报错", () => {
    expect(parseBatchText("  \n ")).toEqual({ entries: [], errors: [] });
  });

  it("JSON 数组按元素序号校验", () => {
    const { entries, errors } = parseBatchText(JSON.stringify([
      { provider: "codebuddy", credential: { token: "a" }, nickname: "n", tags: ["x"] },
      { provider: "", credential: {} },
      { provider: "qoder", credential: "text" },
      "not-object",
    ]));
    expect(entries).toHaveLength(1);
    expect(entries[0]).toMatchObject({ provider: "codebuddy", nickname: "n", tags: ["x"] });
    expect(errors.map((e) => e.line)).toEqual([2, 3, 4]);
  });

  it("JSONL 逐行解析，坏行只算自己", () => {
    const text = [
      '{"provider":"trae","credential":{"accessToken":"a"}}',
      "{broken",
      "",
      '{"provider":"zen","credential":{"session":"s"}}',
    ].join("\n");
    const { entries, errors } = parseBatchText(text);
    expect(entries.map((e) => e.provider)).toEqual(["trae", "zen"]);
    expect(errors).toEqual([{ line: 2, message: "JSON 解析失败" }]);
  });

  it("顶层不是数组 / JSON 坏了直接报错", () => {
    expect(parseBatchText('{"credential":{}}').errors[0].message).toBe("缺少 provider");
    expect(parseBatchText("[{").errors).toEqual([{ line: 1, message: "JSON 解析失败" }]);
  });
});

describe("BatchImportDialog", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  const entry = { provider: "codebuddy", credential: { token: "a" }, nickname: "", tags: [] };

  function renderDialog(overrides: Partial<Parameters<typeof BatchImportDialog>[0]> = {}) {
    const onClose = vi.fn();
    const onImported = vi.fn();
    const onNotice = vi.fn();
    render(
      <BatchImportDialog
        onClose={onClose}
        onImported={onImported}
        onNotice={onNotice}
        {...overrides}
      />,
    );
    return { onClose, onImported, onNotice };
  }

  it("提交成功：汇总并回调刷新", async () => {
    const spy = vi.spyOn(api, "batchImportCredentials").mockResolvedValue({
      created: [{ index: 0, id: "cred_1", tos: { level: "green", reason: "", basis: "" } }],
      skipped: [],
      failed: [],
    });
    const { onImported } = renderDialog();
    fireEvent.change(screen.getByTestId("batch-text"), {
      target: { value: JSON.stringify([entry]) },
    });
    await userEvent.click(screen.getByTestId("batch-submit"));

    await waitFor(() => expect(onImported).toHaveBeenCalledWith("批量导入完成：成功 1 条"));
    expect(screen.getByTestId("batch-result")).toHaveTextContent("成功 1 条");
    spy.mockRestore();
  });

  it("失败行回填成 JSONL，方便直接重试", async () => {
    vi.spyOn(api, "batchImportCredentials").mockResolvedValue({
      created: [],
      skipped: [],
      failed: [{ index: 0, reason: "bad credential" }],
    });
    renderDialog();
    fireEvent.change(screen.getByTestId("batch-text"), {
      target: { value: JSON.stringify([entry]) },
    });
    await userEvent.click(screen.getByTestId("batch-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("batch-text")).toHaveValue(JSON.stringify(entry)));
    expect(screen.getByTestId("batch-result")).toHaveTextContent("失败 1 条");
  });

  it("全部重复时提示且不报成功", async () => {
    vi.spyOn(api, "batchImportCredentials").mockResolvedValue({
      created: [],
      skipped: [{ index: 0, reason: "duplicate" }],
      failed: [],
    });
    const { onImported, onNotice } = renderDialog();
    fireEvent.change(screen.getByTestId("batch-text"), {
      target: { value: JSON.stringify([entry]) },
    });
    await userEvent.click(screen.getByTestId("batch-submit"));

    await waitFor(() => expect(onNotice).toHaveBeenCalledWith("全部为重复凭证（1 条），未新增"));
    expect(onImported).not.toHaveBeenCalled();
  });

  it("请求失败显示后端文案", async () => {
    vi.spyOn(api, "batchImportCredentials").mockRejectedValue(
      new ApiError(400, "invalid_request", "items must be a non-empty array"),
    );
    renderDialog();
    fireEvent.change(screen.getByTestId("batch-text"), {
      target: { value: JSON.stringify([entry]) },
    });
    await userEvent.click(screen.getByTestId("batch-submit"));

    await waitFor(() => expect(screen.getByTestId("batch-error"))
      .toHaveTextContent("items must be a non-empty array"));
  });

  it("没有可导入条目时提交被禁用", () => {
    renderDialog();
    expect(screen.getByTestId("batch-submit")).toBeDisabled();
  });

  it("解析错误提示行号", () => {
    renderDialog();
    fireEvent.change(screen.getByTestId("batch-text"), { target: { value: "{broken" } });
    expect(screen.getByTestId("batch-parse-errors")).toHaveTextContent("第 1 行");
  });

  it("关闭按钮回调", async () => {
    const { onClose } = renderDialog();
    await userEvent.click(screen.getByTestId("batch-cancel"));
    expect(onClose).toHaveBeenCalled();
  });
});
