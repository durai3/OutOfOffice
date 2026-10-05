import * as React from "react";
import { cn } from "@/lib/utils";

export function Label({ className, ...props }: React.ComponentProps<"label">) {
  return <label className={cn("text-xs font-medium text-foreground/90", className)} {...props} />;
}

export function Hint({ className, ...props }: React.ComponentProps<"p">) {
  return <p className={cn("text-xs text-muted-foreground", className)} {...props} />;
}
