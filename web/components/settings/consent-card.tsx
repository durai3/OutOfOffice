"use client";

import * as React from "react";
import { AlertTriangle, Check, ShieldAlert } from "lucide-react";
import { api, errorMessage } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";
import { useToast } from "@/components/toast";
import type { ConsentMode, SettingsResponse } from "@/lib/types";

export function ConsentCard({ settings, onSaved }: { settings: SettingsResponse; onSaved: () => void }) {
  const toast = useToast();
  const [mode, setMode] = React.useState<ConsentMode>(settings.preferences.consent_mode);
  const [timeout, setTimeoutSeconds] = React.useState(settings.preferences.escalation_timeout_seconds);
  const [busy, setBusy] = React.useState(false);

  async function save() {
    setBusy(true);
    try {
      await api.savePreferences({ consent_mode: mode, escalation_timeout_seconds: timeout });
      onSaved();
      toast.push({ title: "Saved", description: "Applies to the next meeting you dispatch." });
    } catch (err) {
      toast.push({ title: "Could not save", description: errorMessage(err), variant: "error" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ShieldAlert className="size-4" aria-hidden="true" /> Recording consent
        </CardTitle>
        <CardDescription>
          Your delegate always announces itself. This decides whether it also waits for the room to agree.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-2">
          {settings.consent_modes.map((option) => (
            <label
              key={option.key}
              className={cn(
                "flex cursor-pointer items-start gap-3 rounded-xl border p-3.5 transition-colors",
                mode === option.key ? "border-primary bg-accent" : "border-border hover:bg-accent/50",
              )}
            >
              <input
                type="radio"
                name="consent-mode"
                checked={mode === option.key}
                onChange={() => setMode(option.key)}
                className="mt-1 accent-[var(--primary)]"
              />
              <span className="min-w-0 flex-1">
                <span className="text-sm font-medium">{option.name}</span>
                <span className="mt-0.5 block text-xs text-muted-foreground">{option.summary}</span>
              </span>
              {mode === option.key ? <Check className="mt-0.5 size-4 shrink-0 text-primary" aria-hidden="true" /> : null}
            </label>
          ))}
        </div>
        <p className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 p-2.5 text-[11px] text-muted-foreground">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-warning-foreground" aria-hidden="true" />
          These are tools to help you comply, not legal advice. You are responsible for having the right to bring a
          delegate to a given meeting.
        </p>
        <div className="space-y-1.5">
          <Label htmlFor="timeout">Approval window (seconds)</Label>
          <Input
            id="timeout"
            type="number"
            min={5}
            max={300}
            value={timeout}
            onChange={(e) => setTimeoutSeconds(Number(e.target.value))}
          />
          <p className="text-[11px] text-muted-foreground">
            How long the delegate waits for you on Telegram before falling back to its holding reply.
          </p>
        </div>
        <Button size="sm" onClick={save} loading={busy}>
          Save
        </Button>
      </CardContent>
    </Card>
  );
}
