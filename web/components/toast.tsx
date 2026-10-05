"use client";

import * as React from "react";
import { AlertTriangle, CheckCircle2, Info, X } from "lucide-react";
import { cn } from "@/lib/utils";

type ToastVariant = "success" | "error" | "info";

interface Toast {
  id: number;
  title: string;
  description?: string;
  variant: ToastVariant;
}

interface ToastInput {
  title: string;
  description?: string;
  variant?: ToastVariant;
  durationMs?: number;
}

const ToastContext = React.createContext<{ push: (toast: ToastInput) => void } | null>(null);

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = React.useState<Toast[]>([]);
  const counter = React.useRef(0);

  const dismiss = React.useCallback((id: number) => setToasts((items) => items.filter((t) => t.id !== id)), []);

  const push = React.useCallback(
    ({ title, description, variant = "info", durationMs }: ToastInput) => {
      counter.current += 1;
      const id = counter.current;
      setToasts((items) => [...items.slice(-4), { id, title, description, variant }]);
      window.setTimeout(() => dismiss(id), durationMs ?? (variant === "error" ? 8000 : 4500));
    },
    [dismiss],
  );

  const value = React.useMemo(() => ({ push }), [push]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="pointer-events-none fixed bottom-4 right-4 z-[60] flex w-[min(380px,calc(100vw-2rem))] flex-col gap-2" aria-live="polite">
        {toasts.map((toast) => (
          <div
            key={toast.id}
            role="status"
            className={cn(
              "pointer-events-auto flex items-start gap-3 rounded-lg border bg-card p-3 shadow-lg fade-in",
              toast.variant === "error" ? "border-danger/40" : toast.variant === "success" ? "border-success/40" : "border-border",
            )}
          >
            {toast.variant === "success" ? (
              <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-success" />
            ) : toast.variant === "error" ? (
              <AlertTriangle className="mt-0.5 size-4 shrink-0 text-danger" />
            ) : (
              <Info className="mt-0.5 size-4 shrink-0 text-info" />
            )}
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium leading-5">{toast.title}</p>
              {toast.description ? <p className="mt-0.5 break-words text-xs text-muted-foreground">{toast.description}</p> : null}
            </div>
            <button type="button" onClick={() => dismiss(toast.id)} className="rounded p-0.5 text-muted-foreground hover:text-foreground" aria-label="Dismiss">
              <X className="size-3.5" />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  const context = React.useContext(ToastContext);
  if (!context) throw new Error("useToast must be used inside <ToastProvider>");
  return context;
}
