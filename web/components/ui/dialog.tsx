"use client";

import * as React from "react";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

interface DialogProps {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
  size?: "md" | "lg" | "xl";
}

const sizes = { md: "max-w-md", lg: "max-w-2xl", xl: "max-w-4xl" };

export function Dialog({ open, onClose, title, description, children, footer, size = "md" }: DialogProps) {
  const panelRef = React.useRef<HTMLDivElement>(null);
  const titleId = React.useId();

  React.useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const focusable = panelRef.current?.querySelector<HTMLElement>("input, textarea, select, button:not([data-dialog-close])");
    focusable?.focus();
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previous;
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center p-4 sm:items-center" role="presentation">
      <div className="absolute inset-0 bg-black/40 backdrop-blur-[2px] fade-in" onClick={onClose} aria-hidden="true" />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={cn("relative z-10 flex max-h-[90vh] w-full flex-col rounded-xl border border-border bg-card shadow-2xl fade-in", sizes[size])}
      >
        <div className="flex items-start justify-between gap-4 border-b border-border p-5 pb-4">
          <div>
            <h2 id={titleId} className="text-base font-semibold tracking-tight">{title}</h2>
            {description ? <p className="mt-1 text-xs text-muted-foreground">{description}</p> : null}
          </div>
          <Button variant="ghost" size="icon" onClick={onClose} aria-label="Close" data-dialog-close className="-mr-2 -mt-2 size-8">
            <X className="size-4" />
          </Button>
        </div>
        <div className="scroll-thin flex-1 overflow-y-auto p-5">{children}</div>
        {footer ? <div className="flex flex-wrap items-center justify-end gap-2 border-t border-border p-4">{footer}</div> : null}
      </div>
    </div>
  );
}
