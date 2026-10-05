"use client";

import * as React from "react";
import Link from "next/link";
import { api, errorMessage } from "@/lib/api";
import { useApi } from "@/hooks/use-api";
import { useAuth } from "@/lib/auth";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";

export function LegalStep({ onDone }: { onDone: () => Promise<void> | void }) {
  const { reload } = useAuth();
  const status = useApi(api.legalStatus);
  const documents = useApi(api.legalDocuments);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  async function accept() {
    setBusy(true);
    setError(null);
    try {
      await api.acceptLegal();
      await reload();
      await onDone();
      await status.refresh();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  if (status.loading || documents.loading) return <Spinner />;
  const outstanding = status.data?.outstanding ?? [];

  return (
    <div className="space-y-3 text-sm">
      {(documents.data ?? []).map((doc) => (
        <div key={doc.key} className="flex items-center justify-between gap-3 rounded-md border border-border p-3">
          <div className="min-w-0">
            <Link href={`/legal/${doc.key}`} target="_blank" className="font-medium underline">
              {doc.title}
            </Link>
            <p className="mt-0.5 text-xs text-muted-foreground">{doc.summary}</p>
          </div>
          <span className="shrink-0 text-xs text-muted-foreground">
            {status.data?.accepted_versions[doc.key] === doc.version ? "accepted" : `v${doc.version}`}
          </span>
        </div>
      ))}
      <p className="text-xs text-muted-foreground">
        The important clause: <strong className="text-foreground">you</strong> decide which meetings your delegate
        joins, and you are responsible for having the right to send it there.
      </p>
      {error ? <p className="text-xs text-danger">{error}</p> : null}
      {outstanding.length ? (
        <Button onClick={accept} loading={busy}>
          Accept and continue
        </Button>
      ) : (
        <p className="text-xs text-success">Accepted. Nothing outstanding.</p>
      )}
    </div>
  );
}
