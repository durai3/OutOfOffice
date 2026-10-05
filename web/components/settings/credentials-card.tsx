"use client";

import * as React from "react";
import { Check, ExternalLink, KeyRound, Trash2 } from "lucide-react";
import { api, errorMessage } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/toast";
import type { CredentialSummary, SettingsResponse } from "@/lib/types";

export function CredentialsCard({ settings, onSaved }: { settings: SettingsResponse; onSaved: () => void }) {
  const toast = useToast();
  const [rows, setRows] = React.useState<CredentialSummary[]>(settings.credentials);
  const [drafts, setDrafts] = React.useState<Record<string, string>>({});
  const [busy, setBusy] = React.useState<string | null>(null);

  React.useEffect(() => setRows(settings.credentials), [settings.credentials]);

  async function store(provider: string) {
    const key = (drafts[provider] ?? "").trim();
    if (!key) return;
    setBusy(provider);
    try {
      setRows(await api.storeKey(provider, key, true));
      setDrafts((d) => ({ ...d, [provider]: "" }));
      onSaved();
      toast.push({ title: "Key stored", description: `${provider} calls now go out on your key.` });
    } catch (err) {
      toast.push({ title: "Key rejected", description: errorMessage(err), variant: "error" });
    } finally {
      setBusy(null);
    }
  }

  async function remove(provider: string) {
    setBusy(provider);
    try {
      setRows(await api.deleteKey(provider));
      onSaved();
      toast.push({ title: "Key removed", description: "Falling back to the shared key and its allowance." });
    } catch (err) {
      toast.push({ title: "Could not remove it", description: errorMessage(err), variant: "error" });
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <KeyRound className="size-4" aria-hidden="true" /> API keys
        </CardTitle>
        <CardDescription>
          Bring your own and your usage stops counting against the {settings.plan_name} allowance. Keys are encrypted
          at rest and checked with one real call before they are saved.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {rows.map((row) => (
          <div key={row.provider} className="space-y-2 rounded-md border border-border p-3">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <p className="text-sm font-medium">{row.label}</p>
                <p className="text-[11px] text-muted-foreground">
                  {row.own_key
                    ? `your key ${row.hint}`
                    : row.configured
                      ? "using the shared key (counts against your allowance)"
                      : "no key configured"}
                  {row.verified_at ? " · verified" : ""}
                </p>
              </div>
              <a
                href={row.help_url}
                target="_blank"
                rel="noreferrer"
                className="shrink-0 text-[11px] text-muted-foreground underline hover:text-foreground"
              >
                get one <ExternalLink className="inline size-3" aria-hidden="true" />
              </a>
            </div>
            <div className="flex gap-2">
              <Input
                type="password"
                placeholder={row.own_key ? "Replace this key" : "Paste your key"}
                value={drafts[row.provider] ?? ""}
                onChange={(e) => setDrafts((d) => ({ ...d, [row.provider]: e.target.value }))}
                className="h-8 text-xs"
              />
              <Button size="sm" onClick={() => store(row.provider)} loading={busy === row.provider} className="shrink-0">
                <Check className="size-3.5" /> Save
              </Button>
              {row.own_key ? (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => remove(row.provider)}
                  loading={busy === row.provider}
                  className="shrink-0"
                  aria-label={`Remove your ${row.provider} key`}
                >
                  <Trash2 className="size-3.5" />
                </Button>
              ) : null}
            </div>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
