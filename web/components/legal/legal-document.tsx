"use client";

import * as React from "react";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { api } from "@/lib/api";
import { useApi } from "@/hooks/use-api";
import { Markdown } from "@/components/ui/markdown";
import { Spinner } from "@/components/ui/spinner";

export function LegalDocumentView({ documentKey }: { documentKey: string }) {
  const fetcher = React.useCallback(() => api.legalDocument(documentKey), [documentKey]);
  const { data, loading, error } = useApi(fetcher, { key: documentKey });

  return (
    <div className="mx-auto w-full max-w-3xl px-4 py-10">
      <Link href="/legal" className="mb-6 inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-3.5" aria-hidden="true" /> All documents
      </Link>
      {loading ? <Spinner /> : null}
      {error ? <p className="text-sm text-danger">{error}</p> : null}
      {data ? (
        <div className="rounded-xl border border-border bg-card p-6 sm:p-8">
          <p className="font-mono text-[10.5px] uppercase tracking-[0.12em] text-muted-foreground">
            Version {data.version} · effective {data.effective_date}
          </p>
          <Markdown className="mt-4">{data.body}</Markdown>
        </div>
      ) : null}
    </div>
  );
}
