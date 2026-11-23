import { Check, Copy } from "lucide-react";
import { Button } from "../../ui";

/** 复制按钮：文案自带「已复制」态，成功/失败由调用方决定 label。 */
export function CopyButton({
  label,
  onCopy,
  testid,
}: {
  label: string;
  onCopy: () => void;
  testid?: string;
}) {
  return (
    <Button
      size="sm"
      variant="ghost"
      data-testid={testid}
      onClick={onCopy}
      className="gap-1"
    >
      {label === "已复制" ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
      {label}
    </Button>
  );
}
