"use client";

import * as React from "react";
import Link from "next/link";
import { api, errorMessage } from "@/lib/api";
import { useApi } from "@/hooks/use-api";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";
import type { PolicyTier } from "@/lib/types";

const TIER_STYLE: Record<PolicyTier, string> = {
  GREEN: "text-success",
  YELLOW: "text-warning",
  RED: "text-danger",
};

const TIER_MEANING: Record<PolicyTier, string> = {
  GREEN: "answers on its own",
  YELLOW: "asks you first",
  RED: "never answers",
};

export function PolicyStep({ onDone }: { onDone: () => Promise<void> | void }) {
  const templates = useApi(api.policyTemplates);
  const profile = useApi(api.profile);
  const [busy, setBusy] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  async function choose(template: string | null) {
    setBusy(template ?? "confirm");
    setError(null);
    try {
      await api.choosePolicy(template);
      await profile.refresh();
      await onDone();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  if (templates.loading || profile.loading) return <Spinner />;
  const rules = profile.data?.policy ?? [];
  const byTier = (tier: PolicyTier) => rules.filter((r) => r.tier === tier);

  return (
    <div className="space-y-4 text-sm">
      <div>
        <p className="font-medium">What your delegate will do right now</p>
        <div className="mt-2 grid gap-2 sm:grid-cols-3">
          {(["GREEN", "YELLOW", "RED"] as PolicyTier[]).map((tier) => (
            <div key={tier} className="rounded-md border border-border p-3">
              <p className={cn("text-xs font-semibold", TIER_STYLE[tier])}>
                {tier} · {TIER_MEANING[tier]}
              </p>
              <ul className="mt-1.5 space-y-0.5 text-xs text-muted-foreground">
                {byTier(tier).map((rule) => (
                  <li key={rule.topic}>{rule.topic.replace(/_/g, " ")}</li>
                ))}
                {byTier(tier).length === 0 ? <li className="italic">nothing</li> : null}
              </ul>
            </div>
          ))}
        </div>
        <p className="mt-2 text-xs text-muted-foreground">
          Every rule is editable on the{" "}
          <Link href="/policy" className="underline hover:text-foreground">Policy</Link> page.
        </p>
      </div>

      <div>
        <p className="font-medium">Start from a different template</p>
        <div className="mt-2 space-y-2">
          {(templates.data ?? []).map((template) => (
            <div key={template.key} className="flex items-start justify-between gap-3 rounded-md border border-border p-3">
              <div className="min-w-0">
                <p className="text-sm font-medium">{template.name}</p>
                <p className="mt-0.5 text-xs text-muted-foreground">{template.summary}</p>
              </div>
              <Button
                size="sm"
                variant="outline"
                loading={busy === template.key}
                onClick={() => choose(template.key)}
                className="shrink-0"
              >
                Use this
              </Button>
            </div>
          ))}
        </div>
      </div>

      {error ? <p className="text-xs text-danger">{error}</p> : null}
      <Button onClick={() => choose(null)} loading={busy === "confirm"}>
        I have read this, keep my policy
      </Button>
    </div>
  );
}
