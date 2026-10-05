import * as React from "react";
import { cn } from "@/lib/utils";

interface EmptyStateProps {
  icon?: React.ReactNode;
  title: string;
  description?: React.ReactNode;
  action?: React.ReactNode;
  className?: string;
  compact?: boolean;
}

export function EmptyState({ icon, title, description, action, className, compact = false }: EmptyStateProps) {
  return (
    <div className={cn("flex flex-col items-center justify-center text-center", compact ? "gap-1.5 py-6" : "gap-2 py-12", className)}>
      {icon ? <div className="mb-1 text-muted-foreground [&>svg]:size-6">{icon}</div> : null}
      <p className={cn("font-medium", compact ? "text-xs" : "text-sm")}>{title}</p>
      {description ? <p className="max-w-sm text-xs text-muted-foreground">{description}</p> : null}
      {action ? <div className="mt-2">{action}</div> : null}
    </div>
  );
}
