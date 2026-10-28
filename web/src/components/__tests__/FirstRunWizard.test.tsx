import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { FirstRunWizard } from "../FirstRunWizard";

const credentialsMock = vi.hoisted(() => vi.fn());
vi.mock("../../api/hooks", () => ({ useCredentials: credentialsMock }));

const STORAGE_KEY = "coding2api.onboarding.v1:root";

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname + location.search}</div>;
}

function renderWizard({ isAdmin = true, username = "root" } = {}) {
  return render(
    <MemoryRouter>
      <FirstRunWizard username={username} isAdmin={isAdmin} />
      <LocationProbe />
    </MemoryRouter>,
  );
}

describe("FirstRunWizard", () => {
  beforeEach(() => {
    localStorage.clear();
    credentialsMock.mockReset();
    credentialsMock.mockReturnValue({ data: { credentials: [] } });
  });

  it("首次登录且没有凭证时弹向导", () => {
    renderWizard();
    expect(screen.getByTestId("first-run-wizard")).toBeInTheDocument();
    expect(screen.getByText("添加第一个上游账号")).toBeInTheDocument();
    expect(screen.getByTestId("wizard-progress")).toHaveTextContent("第 1 / 3 步");
  });

  it("已有凭证不打扰", () => {
    credentialsMock.mockReturnValue({ data: { credentials: [{ id: "a" }] } });
    renderWizard();
    expect(screen.queryByTestId("first-run-wizard")).not.toBeInTheDocument();
  });

  it("非管理员不显示", () => {
    renderWizard({ isAdmin: false });
    expect(screen.queryByTestId("first-run-wizard")).not.toBeInTheDocument();
    // 非管理员连查询都不该发
    expect(credentialsMock).not.toHaveBeenCalled();
  });

  it("查询未返回时不闪弹窗", () => {
    credentialsMock.mockReturnValue({ data: undefined });
    renderWizard();
    expect(screen.queryByTestId("first-run-wizard")).not.toBeInTheDocument();
  });

  it("跳过后写入 localStorage 并永久隐藏", async () => {
    const { rerender } = renderWizard();
    await userEvent.click(screen.getByTestId("wizard-skip"));
    expect(localStorage.getItem(STORAGE_KEY)).toBe("done");

    rerender(
      <MemoryRouter>
        <FirstRunWizard username="root" isAdmin />
      </MemoryRouter>,
    );
    expect(screen.queryByTestId("first-run-wizard")).not.toBeInTheDocument();
  });

  it("已标记完成的不再出现", () => {
    localStorage.setItem(STORAGE_KEY, "done");
    renderWizard();
    expect(screen.queryByTestId("first-run-wizard")).not.toBeInTheDocument();
    expect(credentialsMock).not.toHaveBeenCalled();
  });

  it("步骤可前进后退，末尾完成写入标记", async () => {
    renderWizard();
    await userEvent.click(screen.getByTestId("wizard-next"));
    expect(screen.getByText("创建 API Key")).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("wizard-next"));
    expect(screen.getByTestId("wizard-curl")).toHaveTextContent("/v1/chat/completions");

    await userEvent.click(screen.getByTestId("wizard-back"));
    expect(screen.getByText("创建 API Key")).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("wizard-next"));
    await userEvent.click(screen.getByTestId("wizard-finish"));
    expect(localStorage.getItem(STORAGE_KEY)).toBe("done");
    expect(screen.queryByTestId("first-run-wizard")).not.toBeInTheDocument();
  });

  it("第一步 CTA 关闭向导并带 ?new=1 跳到凭证页", async () => {
    renderWizard();
    await userEvent.click(screen.getByTestId("wizard-action"));
    expect(screen.getByTestId("location")).toHaveTextContent("/?new=1");
    expect(screen.queryByTestId("first-run-wizard")).not.toBeInTheDocument();
  });

  it("第二步 CTA 跳到 API Key 页", async () => {
    renderWizard();
    await userEvent.click(screen.getByTestId("wizard-next"));
    await userEvent.click(screen.getByTestId("wizard-action"));
    expect(screen.getByTestId("location")).toHaveTextContent("/api-keys?new=1");
  });
});
