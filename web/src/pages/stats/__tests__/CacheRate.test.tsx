/**
 * 缓存命中率的展示口径（上游可不上报缓存字段）。
 *
 * 三条硬规则：
 * 1. `cached_tokens` 为 null = 未上报，命中 / 未命中 / 命中率一律 `—`，
 *    绝不允许把「没数据」渲染成 `0%`。
 * 2. 命中率用后端算好的 `cache_rate`，前端不拿总输入重算。
 * 3. 分组行的分母是 `cached_input_tokens`（只含上报过缓存的请求），
 *    不是总输入。
 * 4. 未上报但该渠道有校准命中率（`cache_rate_assumed`，TRAE）时显示 `≈`，
 *    明确是推算，不是上游回传。
 */
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { userEvent } from "@/test-utils";
import type { StatsOverview } from "../../../api/types";
import { StatsOverviewCards } from "../StatsOverviewCards";
import { GroupStatsPanel } from "../GroupStatsPanel";
import { tokenCell } from "../cells";
import {
  GROUP_DIMENSIONS,
  type GroupRow,
  type StatsSortState,
} from "../constants";
const BASE_OVERVIEW: StatsOverview = {
  requests: 120,
  ok_count: 114,
  success_rate: 0.95,
  input_tokens: 1000,
  output_tokens: 2000,
  reasoning_tokens: 300,
  cached_tokens: 400,
  cached_input_tokens: 1000,
  cache_rate: 0.4,
  credit: 12.5,
  credit_estimated: false,
  cost_usd: 1.84,
  cost_cny: 12.34,
  avg_latency_ms: 850,
  avg_ttfb_ms: 220,
  tps: 94.1,
  tps_visible: 52.0,
  tps_estimated: false,
};

function overview(overrides: Partial<StatsOverview>): StatsOverview {
  return { ...BASE_OVERVIEW, ...overrides };
}

function groupRow(overrides: Partial<GroupRow>): GroupRow {
  return {
    requests: 10,
    ok_count: 10,
    input_tokens: 300,
    output_tokens: 100,
    cached_tokens: 100,
    cached_input_tokens: 300,
    credit: 1,
    credit_estimated: false,
    cost_usd: 0.1,
    cost_cny: 0.7,
    avg_latency_ms: 4200,
    avg_ttfb_ms: 900,
    tps: 100,
    tps_visible: 50,
    tps_estimated: false,
    p95_latency_ms: 9500,
    p95_ttfb_ms: 2400,
    provider: "trae",
    ...overrides,
  };
}

const GROUP_SORT: StatsSortState = {
  sort: "requests",
  order: "desc",
  toggle: () => {},
};

function renderGroups(rows: GroupRow[]) {
  const group = GROUP_DIMENSIONS[0];
  return render(
    <GroupStatsPanel
      groupDimension={group.value}
      groupDimensions={GROUP_DIMENSIONS}
      onGroupDimensionChange={() => {}}
      percentile="p95"
      onPercentileChange={() => {}}
      groupSort={GROUP_SORT}
      group={group}
      groupRows={rows}
      onDrill={() => {}}
    />,
  );
}

describe("StatsOverviewCards 缓存口径", () => {
  it("正常上报：命中 / 未命中按已上报输入算，命中率用后端 cache_rate", () => {
    render(<StatsOverviewCards overviewFailed={false} stats={BASE_OVERVIEW} />);

    const card = screen.getByTestId("stat-cache");
    expect(card).toHaveTextContent("40.0%");
    expect(card).toHaveTextContent("命中 400 · 未命中 600");
    expect(screen.queryByTestId("stat-cache-note")).not.toBeInTheDocument();
    // Token 卡片的 hint 同一口径
    expect(screen.getByText(/输入 1000（命中 400 · 未命中 600）/)).toBeInTheDocument();
  });

  it("上游未上报（TRAE 式）：三项全 —，并说明不代表未命中", () => {
    render(
      <StatsOverviewCards
        overviewFailed={false}
        stats={overview({ cached_tokens: null, cached_input_tokens: null, cache_rate: null })}
      />,
    );

    const card = screen.getByTestId("stat-cache");
    expect(card).toHaveTextContent("—");
    expect(card).not.toHaveTextContent("0.0%");
    expect(card).toHaveTextContent("命中 — · 未命中 —");
    expect(within(card).getByTestId("stat-cache-note"))
      .toHaveTextContent("上游未上报缓存字段，显示 — 不代表未命中");
  });

  it("部分渠道未上报：命中率按已上报请求计算，未命中用 cached_input_tokens", () => {    // 400 命中 ÷ 600 已上报输入 = 66.7%；若拿总输入 1000 会稀释成 40.0%
    render(
      <StatsOverviewCards
        overviewFailed={false}
        stats={overview({
          cached_tokens: 400,
          cached_input_tokens: 600,
          cache_rate: 0.6667,
        })}
      />,
    );

    const card = screen.getByTestId("stat-cache");
    expect(card).toHaveTextContent("66.7%");
    expect(card).toHaveTextContent("命中 400 · 未命中 200");
    expect(within(card).getByTestId("stat-cache-note"))
      .toHaveTextContent("命中率按已上报请求计算");
  });
});

describe("分组行 / 明细行缓存口径", () => {
  it("分组行命中率以 cached_input_tokens 为分母，不是总输入", () => {
    renderGroups([groupRow({ cached_tokens: 100, cached_input_tokens: 200, input_tokens: 300 })]);

    const row = screen.getByTestId("group-row-trae");
    // 100 ÷ 200 = 50.0%；总输入当分母会得到 33.3%
    expect(within(row).getByTestId("group-cache-rate")).toHaveTextContent("50.0%");
  });

  it("分组行未上报缓存：命中率 —，提示未上报", async () => {
    renderGroups([
      groupRow({ cached_tokens: null, cached_input_tokens: null, input_tokens: 300 }),
    ]);

    const row = screen.getByTestId("group-row-trae");
    const rate = within(row).getByTestId("group-cache-rate");
    expect(rate).toHaveTextContent("—");
    await userEvent.hover(rate);
    expect(await screen.findByRole("tooltip")).toHaveTextContent("上游未上报缓存字段");
  });

  it("分组行缺 cached_input_tokens（老接口）：回落到总输入", () => {
    const legacy = groupRow({ cached_tokens: 100, input_tokens: 300 });
    delete (legacy as { cached_input_tokens?: unknown }).cached_input_tokens;
    renderGroups([legacy]);

    const row = screen.getByTestId("group-row-trae");
    expect(within(row).getByTestId("group-cache-rate")).toHaveTextContent("33.3%");
  });

  it("明细行未上报缓存：命中率 — 且提示未上报，不显示 0%", async () => {
    render(
      <table>
        <tbody>
          <tr>
            {tokenCell({
              rowid: 1,
              ts: 1_700_000_000,
              username: "root",
              provider: "trae",
              model: "deepseek-v4",
              credential_id: null,
              credential_name: null,
              ok: 1,
              error_type: null,
              input_tokens: 120,
              output_tokens: 480,
              cached_tokens: null,
              credit: null,
              credit_estimated: 0,
              cost_usd: null,
              cost_cny: null,
              latency_ms: 300,
              ttfb_ms: 150,
              tps: null,
              tps_visible: null,
              request_id: "req",
            })}
          </tr>
        </tbody>
      </table>,
    );

    const cell = document.querySelector<HTMLElement>('[data-slot="tooltip-trigger"]')!;
    expect(cell).toHaveTextContent("120 / 480 · —");
    expect(cell).not.toHaveTextContent("0.0%");
    await userEvent.hover(cell);
    expect(await screen.findByRole("tooltip")).toHaveTextContent("上游未上报缓存字段");
  });
});

describe("未上报但带校准命中率（TRAE ≈ 推算）", () => {
  it("总览：显示 ≈ 实测校准值，并说明是推算", () => {
    render(
      <StatsOverviewCards
        overviewFailed={false}
        stats={overview({
          input_tokens: 1000,
          cached_tokens: null,
          cached_input_tokens: null,
          cache_rate: null,
          cache_rate_assumed: 0.98,
        })}
      />,
    );

    const card = screen.getByTestId("stat-cache");
    expect(card).toHaveTextContent("≈98.0%");
    expect(card).not.toHaveTextContent("0.0%");
    // 命中 / 未命中是 input × 校准比例的推算值
    expect(card).toHaveTextContent("命中 ≈980 · 未命中 ≈20");
    expect(within(card).getByTestId("stat-cache-note"))
      .toHaveTextContent("按校准比例的推算");
  });

  it("分组行：显示 ≈ 命中率并提示上游未上报", async () => {
    renderGroups([
      groupRow({
        input_tokens: 1000,
        cached_tokens: null,
        cached_input_tokens: null,
        cache_rate_assumed: 0.98,
      }),
    ]);

    const row = screen.getByTestId("group-row-trae");
    const rate = within(row).getByTestId("group-cache-rate");
    expect(rate).toHaveTextContent("≈98.0%");
    await userEvent.hover(rate);
    expect(await screen.findByRole("tooltip")).toHaveTextContent("按校准命中率推算");
  });

  it("明细行：显示 ≈ 命中率而不是 —", async () => {
    render(
      <table>
        <tbody>
          <tr>
            {tokenCell({
              rowid: 1,
              ts: 1_700_000_000,
              username: "root",
              provider: "trae",
              model: "deepseek-v4",
              credential_id: null,
              credential_name: null,
              ok: 1,
              error_type: null,
              input_tokens: 1000,
              output_tokens: 100,
              cached_tokens: null,
              cache_rate_assumed: 0.98,
              credit: null,
              credit_estimated: 0,
              cost_usd: null,
              cost_cny: null,
              latency_ms: 300,
              ttfb_ms: 150,
              tps: null,
              tps_visible: null,
              request_id: "req",
            })}
          </tr>
        </tbody>
      </table>,
    );

    const cell = document.querySelector<HTMLElement>('[data-slot="tooltip-trigger"]')!;
    expect(cell).toHaveTextContent("≈98.0%");
    expect(cell).not.toHaveTextContent(" · —");
    await userEvent.hover(cell);
    expect(await screen.findByRole("tooltip")).toHaveTextContent("按校准命中率推算");
  });
});
