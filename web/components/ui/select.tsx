import * as React from "react";
import { ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";

export function Select({ className, children, ...props }: React.ComponentProps<"select">) {
  return (
    <div className="relative">
      <select
        className={cn(
          "flex h-9 w-full appearance-none rounded-md border border-input bg-card px-3 pr-8 text-sm text-foreground shadow-sm",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50",
          className,
        )}
        {...props}
      >
        {children}
      </select>
      <ChevronDown className="pointer-events-none absolute right-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
    </div>
  );
}
