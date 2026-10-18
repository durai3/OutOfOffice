"use client";

/**
 * The four-step first run: terms, policy, phone, documents.
 *
 * The order is not cosmetic. A delegate that can be dispatched before its
 * principal has read what it is allowed to say is the single most likely way
 * this product says something embarrassing, so the policy review step is not
 * skippable even though the policy already exists. Connecting a phone is
 * skippable and says exactly what is lost by skipping it.
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import { ArrowRight, Check, Loader2 } from "lucide-react";
import { api, errorMessage } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Spinner } from "@/components/ui/spinner";
import { useToast } from "@/components/toast";
import { LegalStep } from "@/components/onboarding/step-legal";
import { PolicyStep } from "@/components/onboarding/step-policy";
import { TelegramStep } from "@/components/onboarding/step-telegram";
import { DocumentsStep } from "@/components/onboarding/step-documents";
import type { OnboardingState } from "@/lib/types";

export function OnboardingWizard() {
  const router = useRouter();
  const toast = useToast();
  const { reload } = useAuth();
  const [state, setState] = React.useState<OnboardingState | null>(null);
  const [active, setActive] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    try {
      const next = await api.onboarding();
      setState(next);
      // Open the first unfinished step on arrival, and move on as soon as the one
      // being worked on is done - otherwise a finished step stays open and the
      // next one looks like it has been forgotten.
      setActive((current) => {
        const open = next.steps.find((step) => step.key === current);
        if (current === null || open?.complete) return next.next_step ?? current ?? next.steps[0]?.key ?? null;
        return current;
      });
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  React.useEffect(() => {
    void load();
  }, [load]);

  const finish = React.useCallback(async () => {
    setBusy(true);
    try {
      await api.completeOnboarding();
      await reload();
      toast.push({ title: "You're set up", description: "Dispatch your delegate from the Overview page." });
      router.replace("/");
    } catch (err) {
      toast.push({ title: "Not quite done", description: errorMessage(err), variant: "error" });
      await load();
    } finally {
      setBusy(false);
    }
  }, [reload, router, toast, load]);

  if (error) {
    return <p className="mx-auto max-w-2xl px-4 py-10 text-sm text-danger">{error}</p>;
  }
  if (!state) {
    return (
      <div className="grid min-h-[50vh] place-items-center">
        <Spinner />
      </div>
    );
  }

  const remaining = state.steps.filter((s) => !s.complete && !s.optional);
  const done = state.steps.length ? state.steps.filter((s) => s.complete).length : 0;
  const progress = state.steps.length ? Math.round((done / state.steps.length) * 100) : 0;

  return (
    <div className="mx-auto w-full max-w-3xl px-4 py-10">
      <p className="font-mono text-[10.5px] uppercase tracking-[0.12em] text-muted-foreground">Onboarding</p>
      <h1 className="mt-1.5 text-xl font-semibold tracking-tight">Set up your delegate</h1>
      <p className="mt-1 text-sm text-muted-foreground">
        Four things, and the last two take a minute each. {done} of {state.steps.length} done.
      </p>

      <div className="mt-4 h-1.5 w-full overflow-hidden rounded-full bg-muted" aria-hidden="true">
        <div
          className="h-full rounded-full bg-primary transition-[width] duration-300"
          style={{ width: `${progress}%` }}
        />
      </div>

      <div className="mt-6 space-y-3">
        {state.steps.map((step, index) => {
          const open = active === step.key;
          return (
            <Card key={step.key} className={cn("overflow-hidden transition-shadow", open && "ring-1 ring-ring")}>
              <button
                type="button"
                onClick={() => setActive(open ? null : step.key)}
                className="w-full text-left"
                aria-expanded={open}
              >
                <CardHeader className="flex-row items-start gap-3">
                  <span
                    className={cn(
                      "mt-0.5 grid size-7 shrink-0 place-items-center rounded-full border text-xs font-semibold tabular-nums",
                      step.complete
                        ? "border-success bg-success text-white"
                        : "border-border text-muted-foreground",
                    )}
                    aria-hidden="true"
                  >
                    {step.complete ? <Check className="size-3.5" /> : index + 1}
                  </span>
                  <span className="min-w-0 flex-1">
                    <CardTitle className="flex items-center gap-2 text-sm">
                      {step.title}
                      {step.optional ? (
                        <Badge variant="outline" className="font-normal">
                          optional
                        </Badge>
                      ) : null}
                    </CardTitle>
                    <CardDescription className="mt-0.5">{step.description}</CardDescription>
                    {step.detail ? (
                      <CardDescription className="mt-1 font-mono text-[10px]">{step.detail}</CardDescription>
                    ) : null}
                  </span>
                </CardHeader>
              </button>
              {open ? (
                <CardContent className="border-t border-border pt-4">
                  {step.key === "legal" ? <LegalStep onDone={load} /> : null}
                  {step.key === "policy" ? <PolicyStep onDone={load} /> : null}
                  {step.key === "escalation" ? <TelegramStep link={state.telegram} onDone={load} /> : null}
                  {step.key === "documents" ? <DocumentsStep onDone={load} /> : null}
                </CardContent>
              ) : null}
            </Card>
          );
        })}
      </div>

      <div className="mt-6 flex items-center justify-between gap-3">
        <p className="text-xs text-muted-foreground">
          {remaining.length
            ? `Still needed: ${remaining.map((s) => s.title.toLowerCase()).join(", ")}.`
            : "Everything required is done."}
        </p>
        <Button size="lg" onClick={finish} disabled={remaining.length > 0} loading={busy}>
          {busy ? <Loader2 className="size-4 animate-spin" /> : null}
          Go to the console <ArrowRight className="size-4" />
        </Button>
      </div>
    </div>
  );
}
