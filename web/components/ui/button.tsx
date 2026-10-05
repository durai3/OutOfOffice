import * as React from "react";
import { cn } from "@/lib/utils";
import { Spinner } from "@/components/ui/spinner";

type Variant = "default" | "secondary" | "outline" | "ghost" | "destructive" | "link";
type Size = "sm" | "md" | "lg" | "icon";

const variants: Record<Variant, string> = {
  default: "bg-primary text-primary-foreground hover:bg-primary/90 shadow-sm",
  secondary: "bg-muted text-foreground hover:bg-accent",
  outline: "border border-border bg-card text-foreground hover:bg-accent",
  ghost: "text-foreground hover:bg-accent",
  destructive: "bg-destructive text-destructive-foreground hover:bg-destructive/90 shadow-sm",
  link: "text-info underline-offset-4 hover:underline px-0 h-auto",
};

const sizes: Record<Size, string> = {
  sm: "h-8 px-3 text-xs gap-1.5",
  md: "h-9 px-4 text-sm gap-2",
  lg: "h-11 px-6 text-sm gap-2",
  icon: "size-9 p-0",
};

export interface ButtonProps extends React.ComponentProps<"button"> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
}

/** Class list for a button-looking element (use on <Link> or <a>). */
export function buttonVariants({ variant = "default", size = "md", className }: { variant?: Variant; size?: Size; className?: string } = {}): string {
  return cn(
    "inline-flex items-center justify-center whitespace-nowrap rounded-md font-medium transition-colors",
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
    "disabled:pointer-events-none disabled:opacity-50",
    variants[variant],
    sizes[size],
    className,
  );
}

export function Button({ className, variant = "default", size = "md", loading = false, disabled, children, type = "button", ...props }: ButtonProps) {
  return (
    <button
      type={type}
      className={buttonVariants({ variant, size, className })}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...props}
    >
      {loading ? <Spinner className="size-4" /> : null}
      {children}
    </button>
  );
}
