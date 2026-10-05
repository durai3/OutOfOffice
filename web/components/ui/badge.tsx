import * as React from "react";
import { cn } from "@/lib/utils";

export type BadgeVariant = "default" | "secondary" | "outline" | "success" | "warning" | "danger" | "info" | "violet";

const variants: Record<BadgeVariant, string> = {
  default: "bg-primary text-primary-foreground border-transparent",
  secondary: "bg-muted text-foreground border-transparent",
  outline: "border-border text-foreground",
  success: "border-transparent bg-success/15 text-success",
  warning: "border-transparent bg-warning/20 text-warning-foreground",
  danger: "border-transparent bg-danger/15 text-danger",
  info: "border-transparent bg-info/15 text-info",
  violet: "border-transparent bg-violet/15 text-violet",
};

export interface BadgeProps extends React.ComponentProps<"span"> {
  variant?: BadgeVariant;
  dot?: boolean;
  pulse?: boolean;
}

export function Badge({ className, variant = "secondary", dot = false, pulse = false, children, ...props }: BadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium leading-4 tracking-wide",
        variants[variant],
        className,
      )}
      {...props}
    >
      {dot ? <span className={cn("size-1.5 rounded-full bg-current", pulse && "pulse-dot")} aria-hidden="true" /> : null}
      {children}
    </span>
  );
}
