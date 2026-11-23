import { useState } from "react";
import { usePlaygroundModels } from "../../api/hooks";
import { buildSections, displayName, modelValue } from "../../components/ModelPicker";
import { Button as RawButton } from "../../components/ui/button";
import { buildCcSwitchLink, CCSWITCH_TARGETS } from "../../lib/ccSwitch";
import { Button, Field, Notice, Select } from "../../ui";

/**
 * 默认模型下拉：数据源与 Playground 同源（`/api/playground/models`），
 * 按渠道分组（多渠道置顶 + 「仅 XX」），值语义与 Playground 一致
 * （多渠道裸 id 走自动路由、单渠道 `id@provider` 强制指定）。
 */
function ModelSelect({
  value,
  onChange,
  testid,
  id,
}: {
  value: string;
  onChange: (value: string) => void;
  testid: string;
  /** 供外层 `Field` 的 htmlFor 关联（读屏报字段名、点 label 聚焦控件）。 */
  id?: string;
}) {
  const modelsQuery = usePlaygroundModels();
  const fetched = modelsQuery.data?.data ?? [];
  const models = fetched.map((item) => ({ ...item, value: modelValue(item) }));
  const sections = buildSections(models);

  return (
    <Select
      id={id}
      value={value}
      data-testid={testid}
      onChange={(event) => onChange(event.target.value)}
    >
      <option value="">不写入（留空）</option>
      {sections.map((section) => (
        <optgroup key={section.key} label={section.label}>
          {section.items.map((item) => (
            <option key={item.value} value={item.value}>
              {displayName(item)}
            </option>
          ))}
        </optgroup>
      ))}
    </Select>
  );
}

/**
 * CC Switch 导入按钮组：渲染在「明文 Key 存活」的上下文里——创建成功后的对话框，
 * 或列表行的导入对话框（用户自己粘贴 Key）。深链含 Key 明文，故只在用户主动
 * 打开、且已给出 Key 时生成。
 */
export function CcSwitchImportButtons({
  apiKey,
  testidPrefix = "ccswitch",
  warning = "深链含 API Key 明文，勿分享、勿在不可信环境使用。",
}: {
  apiKey: string;
  /** data-testid 前缀：列表行导入对话框用 `ccswitch-row`，避免与创建对话框撞名。 */
  testidPrefix?: string;
  warning?: string;
}) {
  const [model, setModel] = useState("");
  const origin = window.location.origin;
  const ready = apiKey.trim() !== "";

  return (
    <div className="space-y-3" data-testid={`${testidPrefix}-import`}>
      <Field
        label="默认模型（可选）"
        htmlFor={`${testidPrefix}-model-select`}
        hint="留空不写入。Claude Code 无模型名时回落内置列表；非原生模型建议选本服务模型。"
      >
        <ModelSelect
          value={model}
          onChange={setModel}
          testid={`${testidPrefix}-model`}
          id={`${testidPrefix}-model-select`}
        />
      </Field>
      <div className="flex flex-wrap gap-2">
        {CCSWITCH_TARGETS.map((target) =>
          ready ? (
            <RawButton key={target.app} size="sm" variant="default" asChild>
              <a
                href={buildCcSwitchLink(target, { origin, apiKey, model })}
                data-testid={`${testidPrefix}-${target.app}`}
              >
                导入到 {target.label}
              </a>
            </RawButton>
          ) : (
            <Button
              key={target.app}
              size="sm"
              variant="default"
              disabled
              data-testid={`${testidPrefix}-${target.app}`}
            >
              导入到 {target.label}
            </Button>
          ),
        )}
      </div>
      <Notice tone="warn">{warning}</Notice>
    </div>
  );
}
