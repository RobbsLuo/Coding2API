import { ChevronDown } from "lucide-react";
import { QUOTA_FIELDS, type KeyQuota, quotaSummary } from "../../api/keyQuota";
import { Collapsible, CollapsibleContent, CollapsibleTrigger, Field, Input } from "../../ui";

/** 六项配额输入：创建对话框与「改配额」对话框共用。 */
export function QuotaFields({
  value,
  onChange,
  testidPrefix,
}: {
  value: KeyQuota;
  onChange: (next: KeyQuota) => void;
  testidPrefix: string;
}) {
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2" data-testid={`${testidPrefix}-fields`}>
      {QUOTA_FIELDS.map((field) => (
        <Field key={field.key} label={field.label} hint={field.hint}
               htmlFor={`${testidPrefix}-${field.key}`}>
          <Input
            id={`${testidPrefix}-${field.key}`}
            type="number"
            min={0}
            step={field.step ?? "1"}
            value={value[field.key] === 0 ? "" : String(value[field.key])}
            placeholder="0"
            data-testid={`${testidPrefix}-${field.key}`}
            onChange={(event) => {
              const raw = event.target.value.trim();
              const numeric = raw === "" ? 0 : Number(raw);
              onChange({ ...value, [field.key]: Number.isFinite(numeric) && numeric > 0 ? numeric : 0 });
            }}
          />
        </Field>
      ))}
    </div>
  );
}

/** 配额区块的折叠外壳（低频配置，默认收起，避免创建对话框过长）。 */
export function QuotaSection({
  title,
  value,
  onChange,
  testid,
}: {
  title: string;
  value: KeyQuota;
  onChange: (next: KeyQuota) => void;
  testid: string;
}) {
  return (
    <Collapsible data-testid={testid} className="rounded-lg border border-border px-3 py-2">
      <CollapsibleTrigger
        data-testid={`${testid}-trigger`}
        className="flex w-full cursor-pointer select-none items-center gap-2 text-sm"
      >
        <span>{title}</span>
        <span className="text-xs text-muted-foreground">{quotaSummary(value)}</span>
        <ChevronDown className="ml-auto size-4" />
      </CollapsibleTrigger>
      <CollapsibleContent className="mt-3">
        <QuotaFields value={value} onChange={onChange} testidPrefix={`${testid}-input`} />
        <p className="mt-2 text-xs text-muted-foreground">
          0 = 不限制。每日类按北京时间自然日统计（与凭证限额同一口径）；
          RPM/TPM/并发是进程内计数，重启清零。
        </p>
      </CollapsibleContent>
    </Collapsible>
  );
}
