import { useState } from "react";
import { ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button, Panel } from "../ui";

export interface HelpEntry {
  term: string;
  where: string;
  meaning: string;
}

/**
 * 就地折叠说明卡：把某块领域知识放到它对应的管理页面里，
 * 默认收起、点「说明」展开，避免头部集中一个大而杂的帮助面板。
 */
export function HelpBlock({ title, entries }: { title: string; entries: HelpEntry[] }) {
  const [open, setOpen] = useState(false);

  return (
    <Panel
      title={title}
      action={
        <Button size="sm" variant="ghost" onClick={() => setOpen((value) => !value)}>
          <ChevronDown
            className={cn("size-4 transition-transform", open && "rotate-180")}
          />
          {open ? "收起" : "说明"}
        </Button>
      }
    >
      {open ? (
        <dl className="space-y-2.5">
          {entries.map((entry) => (
            <div key={entry.term} className="rounded-lg border border-border px-3 py-2">
              <dt className="font-medium">{entry.term}</dt>
              <dd className="mt-0.5 text-xs text-muted-foreground">
                位置：{entry.where}
                <div className="mt-1 text-foreground">{entry.meaning}</div>
              </dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="text-sm text-muted-foreground">展开查看细分说明。</p>
      )}
    </Panel>
  );
}