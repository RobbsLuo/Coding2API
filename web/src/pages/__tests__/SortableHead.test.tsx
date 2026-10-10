import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SortableHead } from "../../components/SortableHead";
import { mockFetch, renderPage, settle, userEvent } from "./helpers";
import { AlertsPage } from "../AlertsPage";

/** 渲染一个最小表格，用 SortableHead 表头，验证 aria-sort 与点击回调。 */
function renderHead(props: Partial<React.ComponentProps<typeof SortableHead>> = {}) {
  const calls: string[] = [];
  const utils = render(
    <table>
      <thead>
        <tr>
          <SortableHead
            label="时间"
            columnKey="ts"
            active
            direction="desc"
            onToggle={(key) => calls.push(key)}
            testId="head-ts"
            {...props}
          />
        </tr>
      </thead>
    </table>,
  );
  return { calls, ...utils };
}

describe("SortableHead", () => {
  it("激活列标注 aria-sort 并按方向取箭头；点击回传列键", async () => {
    const { calls } = renderHead();
    const th = screen.getByTestId("head-ts").closest("th")!;
    expect(th).toHaveAttribute("aria-sort", "descending");
    await userEvent.click(screen.getByTestId("head-ts"));
    expect(calls).toEqual(["ts"]);
  });

  it("升序激活列标注 ascending", () => {
    renderHead({ active: true, direction: "asc" });
    expect(screen.getByTestId("head-ts").closest("th")).toHaveAttribute("aria-sort", "ascending");
  });

  it("未激活列 aria-sort=none 且右对齐类生效", () => {
    renderHead({ active: false, align: "right" });
    const th = screen.getByTestId("head-ts").closest("th")!;
    expect(th).toHaveAttribute("aria-sort", "none");
    expect(th).toHaveClass("text-right");
  });
});

describe("AlertsPage 排序", () => {
  it("默认按 ts 降序；点击表头把新 sort/order 带进请求", async () => {
    const spy = mockFetch({ "/api/alerts": { alerts: [
      { id: "alert_1", ts: 1, rule: "pool_empty", severity: "critical",
        scope: "pool", message: "x", delivered: 1, delivery_error: null },
    ] } });
    renderPage(<AlertsPage />, { username: "root", is_admin: true });
    await settle();

    const urls = () => spy.mock.calls.map((call) => String(call[0]));
    // 初始请求已带默认排序（ts 降序，由 useSort 显式下发）
    expect(urls()[0]).toContain("sort=ts");
    expect(urls()[0]).toContain("order=desc");

    await userEvent.click(screen.getByTestId("sort-severity"));
    const last = urls().at(-1)!;
    expect(last).toContain("sort=severity");
    expect(last).toContain("order=desc");
  });
});
