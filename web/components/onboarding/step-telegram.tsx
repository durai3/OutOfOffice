"use client";

import * as React from "react";
import { Check, Copy, RefreshCw } from "lucide-react";
import { api, errorMessage } from "@/lib/api";
import { Button } from "@/components/ui/button";
import type { TelegramLink } from "@/lib/types";

export function TelegramStep({ link, onDone }: { link: TelegramLink; onDone: () => Promise<void> | void }) {
  const [current, setCurrent] = React.useState(link);
  const [busy, setBusy] = React.useState(false);
  const [copied, setCopied] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => setCurrent(link), [link]);

  // Linking happens in Telegram, not here, so the page polls for it rather
  // than making the user click "I've done it".
  React.useEffect(() => {
    if (current.linked) return;
    const id = window.setInterval(() => void onDone(), 4000);
    return () => window.clearInterval(id);
  }, [current.linked, onDone]);

  async function issue() {
    setBusy(true);
    setError(null);
    try {
      setCurrent(await api.telegramCode());
      await onDone();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function skip() {
    setBusy(true);
    try {
      await api.skipStep("escalation");
      await onDone();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  if (current.linked) {
    return (
      <p className="flex items-center gap-2 text-sm text-success">
        <Check className="size-4" aria-hidden="true" /> Connected. Approvals and briefings arrive in Telegram.
      </p>
    );
  }

  const command = `/link ${current.code ?? ""}`;

  return (
    <div className="space-y-3 text-sm">
      <p className="text-muted-foreground">{current.instructions}</p>
      {current.code ? (
        <div className="flex items-center gap-2">
          <code className="flex-1 rounded-md border border-border bg-muted px-3 py-2 font-mono text-sm">{command}</code>
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              void navigator.clipboard.writeText(command);
              setCopied(true);
              window.setTimeout(() => setCopied(false), 1500);
            }}
          >
            {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />} {copied ? "Copied" : "Copy"}
          </Button>
        </div>
      ) : null}
      <p className="text-xs text-muted-foreground">
        Without a channel, anything your policy marks as needing approval gets a safe holding reply instead of
        reaching you. Nothing is ever sent to a chat you have not linked.
      </p>
      {error ? <p className="text-xs text-danger">{error}</p> : null}
      <div className="flex gap-2">
        <Button size="sm" variant="outline" onClick={issue} loading={busy}>
          <RefreshCw className="size-3.5" /> {current.code ? "New code" : "Get a code"}
        </Button>
        <Button size="sm" variant="ghost" onClick={skip} loading={busy}>
          Skip for now
        </Button>
      </div>
    </div>
  );
}
