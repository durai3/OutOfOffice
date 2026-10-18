"use client";

import * as React from "react";
import { AlertTriangle, RefreshCw } from "lucide-react";
import { api, errorMessage } from "@/lib/api";
import type { HealthResponse } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * Says out loud when the delegate is running degraded.
 *
 * This exists because of a failure that was invisible in this console for days:
 * a retired model id made every LLM-backed component fall back to a
 * deterministic path, so the delegate kept "working" - answering questions with
 * unrelated sentences lifted out of PDFs - and nothing anywhere said so. The
 * only trace was a `fallback_reason` field inside an archived briefing's JSON.
 *
 * A degraded delegate that looks healthy is worse than one that is plainly
 * broken, so this banner is deliberately hard to ignore.
 */
export function HealthBanner({
  health,
  onRefresh,
}: {
  health: HealthResponse | undefined;
  onRefresh?: () => void;
}) {
  const [rechecking, setRechecking] = React.useState(false);
  const [recheckError, setRecheckError] = React.useState<string | null>(null);

  if (!health || health.healthy) return null;

  // "Blocking" used to read a planner_ready flag off the health payload. That
  // payload is now unauthenticated and deployment-wide, so it cannot speak for
  // one tenant's model; the LLM degradation itself is the signal, and the
  // re-check below reports on *this account's* configured model.
  const blocking = health.degradations.some((item) => item.component === "llm");

  async function recheck() {
    setRechecking(true);
    setRecheckError(null);
    try {
      await api.recheckLlm();
      onRefresh?.();
    } catch (error) {
      setRecheckError(errorMessage(error));
    } finally {
      setRechecking(false);
    }
  }

  return (
    <div
      role="alert"
      className={cn(
        "mb-5 rounded-lg border px-4 py-3",
        blocking ? "border-danger/50 bg-danger/10" : "border-warning/50 bg-warning/10",
      )}
    >
      <div className="flex flex-wrap items-start gap-3">
        <AlertTriangle className={cn("mt-0.5 size-4 shrink-0", blocking ? "text-danger" : "text-warning")} />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium">
            {blocking
              ? "Your delegate can't think right now, so it won't answer in meetings."
              : "Your delegate is running with reduced abilities."}
          </p>
          <ul className="mt-1.5 space-y-1">
            {health.degradations.map((item) => (
              <li key={item.component} className="text-xs text-muted-foreground">
                <span className="font-mono text-[11px] uppercase tracking-wide">{item.component}</span>
                {" — "}
                {item.summary}
                {item.count > 1 ? <span className="opacity-70"> (×{item.count})</span> : null}
                {item.detail ? (
                  <span className="mt-0.5 block break-words font-mono text-[10px] opacity-70">{item.detail}</span>
                ) : null}
              </li>
            ))}
          </ul>
          {blocking ? (
            <p className="mt-2 text-xs text-muted-foreground">
              Meetings will run without a brain until this clears. Add your own key in{" "}
              <a href="/settings" className="underline">Settings</a> to bypass the shared one, then re-check.
            </p>
          ) : null}
          {recheckError ? <p className="mt-2 text-xs text-danger">{recheckError}</p> : null}
        </div>
        <Button size="sm" variant="outline" onClick={recheck} disabled={rechecking}>
          <RefreshCw className={cn("size-3.5", rechecking && "animate-spin")} />
          {rechecking ? "Checking…" : "Re-check"}
        </Button>
      </div>
    </div>
  );
}
