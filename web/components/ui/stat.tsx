import * as React from "react";
import { cn } from "@/lib/utils";

export function Stat({ label, value, hint, className }: { label: string; value: React.ReactNode; hint?: React.ReactNode; className?: string }) {
  return (
    <div className={cn("flex flex-col gap-0.5", className)}>
      <span className="font-mono text-[10.5px] font-medium uppercase tracking-[0.12em] text-muted-foreground">{label}</span>
      <span className="tabular text-lg font-semibold leading-tight">{value}</span>
      {hint ? <span className="text-xs text-muted-foreground">{hint}</span> : null}
    </div>
  );
}
