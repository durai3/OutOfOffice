"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { AlertCircle, ArrowRight, Zap } from "lucide-react";
import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/api";
import { useAuth } from "@/lib/auth";

/** Where a one-click evaluator lands: the Playground, in judge mode. */
export const JUDGE_LANDING = "/playground?judge=1";

/**
 * "Get straight to the product" - for hackathon judges and anyone else who is
 * evaluating rather than signing up.
 *
 * It creates a throwaway account server-side (`POST /auth/demo`) and drops the
 * visitor into the Playground with sample documents already loading. Ordinary
 * sign-in and sign-up stay exactly as they were, directly below it.
 */
export function JudgeEntry({ className }: { className?: string }) {
  const { demo, status } = useAuth();
  const router = useRouter();
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  async function enter() {
    setBusy(true);
    setError(null);
    try {
      if (status !== "authenticated") await demo();
      router.replace(JUDGE_LANDING);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <div className={className}>
      <div className="rounded-2xl border border-primary/40 bg-primary/10 p-5 shadow-[0_20px_50px_-25px_color-mix(in_oklch,var(--primary)_60%,transparent)]">
        <p className="flex items-center gap-1.5 font-mono text-[10.5px] uppercase tracking-[0.12em] text-primary">
          <Zap className="size-3" aria-hidden="true" /> Hackathon judges
        </p>
        <h2 className="mt-1.5 text-base font-semibold tracking-tight text-foreground">Get straight to the product</h2>
        <p className="mt-1 text-sm leading-relaxed text-muted-foreground">
          No signup, no email, no API key, no meeting link. You get your own private workspace with sample documents, and
          you can run a whole meeting in about two minutes.
        </p>
        <Button type="button" size="lg" className="mt-4 w-full" onClick={enter} loading={busy}>
          {status === "authenticated" ? "Continue to the product" : "Get straight to the product"} <ArrowRight className="size-4" />
        </Button>
        {error ? (
          <p className="mt-3 flex items-start gap-2 rounded-lg border border-danger/40 bg-danger/5 p-2.5 text-xs text-danger">
            <AlertCircle className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
            {error}
          </p>
        ) : null}
        <p className="mt-3 text-[11px] leading-relaxed text-muted-foreground">
          Creates a temporary demo account and accepts the Terms and Privacy Policy on its behalf. Prefer your own account?
          Sign in or create one below.
        </p>
      </div>
    </div>
  );
}
