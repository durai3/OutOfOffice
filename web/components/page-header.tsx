import * as React from "react";

export function PageHeader({ title, description, actions }: { title: string; description?: React.ReactNode; actions?: React.ReactNode }) {
  return (
    <div className="fade-in mb-7 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between sm:gap-6">
      <div>
        <h1 className="text-[1.75rem] font-medium leading-[1.1] tracking-[-0.025em] sm:text-[1.875rem]">{title}</h1>
        {description ? <p className="mt-2 max-w-2xl text-pretty text-sm leading-relaxed text-muted-foreground">{description}</p> : null}
      </div>
      {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  );
}
