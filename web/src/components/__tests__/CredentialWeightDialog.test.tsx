import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { api } from "../../api/client";
import { makeCredential } from "../../test-utils";
import {
  CredentialWeightDialog,
  MAX_WEIGHT,
  MIN_WEIGHT,
  parseWeightInput,
} from "../CredentialWeightDialog";

const credential = makeCredential({ id: "cred_w", nickname: "W" });

function setup(overrides: Partial<Parameters<typeof CredentialWeightDialog>[0]> = {}) {
  const onSaved = vi.fn();
  const onError = vi.fn();
  render(
    <CredentialWeightDialog
      credential={credential as never}
      onClose={vi.fn()}
      onSaved={onSaved}
      onError={onError}
      {...overrides}
    />,
  );
  return { onSaved, onError };
}

describe("parseWeightInput", () => {
  it("空 / default → null，合法整数原样返回", () => {
    expect(parseWeightInput("")).toBeNull();
    expect(parseWeightInput("  ")).toBeNull();
    expect(parseWeightInput("default")).toBeNull();
    expect(parseWeightInput("7")).toBe(7);
    expect(parseWeightInput("-10")).toBe(-10);
  });

  it("非整数 / 越界 / 小数都判非法", () => {
    for (const raw of ["abc", "1.5", "1e3", String(MAX_WEIGHT + 1),
                       String(MIN_WEIGHT - 1)]) {
      expect(parseWeightInput(raw)).toBe("invalid");
    }
  });
});

describe("CredentialWeightDialog", () => {
  it("保存权重并回调成功文案", async () => {
    const saved = vi.spyOn(api, "setCredentialWeight").mockResolvedValue({ routing_weight: 42 });
    const { onSaved } = setup();

    await userEvent.type(screen.getByTestId("credential-weight-input"), "42");
    await userEvent.click(screen.getByTestId("credential-weight-save"));

    await waitFor(() => expect(saved).toHaveBeenCalledWith("cred_w", 42));
    expect(onSaved).toHaveBeenCalledWith(expect.stringContaining("42"));
  });

  it("留空提交 = 恢复默认（null）", async () => {
    const saved = vi.spyOn(api, "setCredentialWeight").mockResolvedValue({ routing_weight: null });
    const { onSaved } = setup();

    await userEvent.click(screen.getByTestId("credential-weight-save"));

    await waitFor(() => expect(saved).toHaveBeenCalledWith("cred_w", null));
    expect(onSaved).toHaveBeenCalledWith(expect.stringContaining("默认"));
  });

  it("非法输入本地拦截，不打后端", async () => {
    const saved = vi.spyOn(api, "setCredentialWeight");
    setup();

    await userEvent.type(screen.getByTestId("credential-weight-input"), "abc");
    await userEvent.click(screen.getByTestId("credential-weight-save"));

    expect(screen.getByTestId("credential-weight-error")).toHaveTextContent("整数");
    expect(saved).not.toHaveBeenCalled();
  });

  it("回填当前权重；0 视为默认留空", () => {
    setup({
      credential: makeCredential({ id: "cred_w", routing_weight: -5 }) as never,
    });
    expect(screen.getByTestId("credential-weight-input")).toHaveValue("-5");
  });

  it("后端报错走 onError，不误报成功", async () => {
    vi.spyOn(api, "setCredentialWeight").mockRejectedValue(new Error("boom"));
    const { onSaved, onError } = setup();

    await userEvent.type(screen.getByTestId("credential-weight-input"), "1");
    await userEvent.click(screen.getByTestId("credential-weight-save"));

    await waitFor(() => expect(onError).toHaveBeenCalledWith("boom"));
    expect(onSaved).not.toHaveBeenCalled();
  });
});
