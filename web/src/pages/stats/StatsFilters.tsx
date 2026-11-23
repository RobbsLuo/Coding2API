/**
 * 筛选条：时间范围 + 图表指标 + 自定义时间窗（自 `pages/StatsPage.tsx` 原样搬出）。
 *
 * 状态仍由页面持有：这里只渲染与回传，不自己存一份，避免两处范围值打偏。
 */
import { Field, Input, Panel, Tabs } from "../../ui";
import { METRICS, RANGES } from "./constants";

interface StatsFiltersProps {
  range: string;
  metric: string;
  customFrom: string;
  customTo: string;
  customInvalid: boolean;
  onRangeChange: (value: string) => void;
  onMetricChange: (value: string) => void;
  onCustomFromChange: (value: string) => void;
  onCustomToChange: (value: string) => void;
}

export function StatsFilters(props: StatsFiltersProps) {
  const {
    range, metric, customFrom, customTo, customInvalid,
    onRangeChange, onMetricChange, onCustomFromChange, onCustomToChange,
  } = props;
  return (
    <>
      {/* 筛选条：时间范围与图表指标合并在同一张卡里——两者都是「决定下面看什么」
          的全局开关，分成两张卡会白占一整块高度和一条分隔线。 */}
      <Panel>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <span className="text-sm font-medium text-muted-foreground">时间范围</span>
          <Tabs
            value={range}
            options={RANGES.map(({ value, label }) => ({ value, label }))}
            onChange={onRangeChange}
            testId="range-tabs"
          />
          <span aria-hidden className="hidden h-4 w-px bg-border sm:block" />
          <span className="text-sm font-medium text-muted-foreground">图表指标</span>
          <Tabs
            value={metric}
            options={METRICS.map(({ value, label }) => ({ value, label }))}
            onChange={onMetricChange}
            testId="metric-tabs"
          />
        </div>
        {/* 自定义范围：两个 datetime-local，精确到分钟、按浏览器本地时区解析。
            起点晚于终点时提示并暂停取数（见 buildCustomRange）。 */}
        {range === "custom" && (
          <div className="mt-3 flex flex-wrap items-end gap-3" data-testid="custom-range">
            <Field label="开始时间" htmlFor="custom-from-input">
              <Input
                id="custom-from-input"
                type="datetime-local"
                value={customFrom}
                data-testid="custom-from"
                className="w-52"
                onChange={(event) => onCustomFromChange(event.target.value)}
              />
            </Field>
            <Field label="结束时间" htmlFor="custom-to-input">
              <Input
                id="custom-to-input"
                type="datetime-local"
                value={customTo}
                data-testid="custom-to"
                className="w-52"
                onChange={(event) => onCustomToChange(event.target.value)}
              />
            </Field>
            {customInvalid && (
              <p className="pb-2 text-xs text-destructive" data-testid="custom-range-invalid">
                开始时间须早于结束时间，且格式有效；已暂停取数。
              </p>
            )}
          </div>
        )}
      </Panel>
    </>
  );
}
