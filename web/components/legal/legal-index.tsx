"use client";

import Link from "next/link";
import { FileText } from "lucide-react";
import { api } from "@/lib/api";
import { useApi } from "@/hooks/use-api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Spinner } from "@/components/ui/spinner";

export function LegalIndex() {
  const { data, loading } = useApi(api.legalDocuments);
  return (
    <div className="mx-auto w-full max-w-3xl px-4 py-10">
      <p className="font-mono text-[10.5px] uppercase tracking-[0.12em] text-muted-foreground">Legal</p>
      <h1 className="mt-1.5 text-xl font-semibold tracking-tight">Legal documents</h1>
      <p className="mt-1 text-sm text-muted-foreground">
        What DELEG8 does with meetings it joins, and what you take responsibility for by sending it.
      </p>
      {loading ? <Spinner className="mt-6" /> : null}
      <div className="mt-6 grid gap-3">
        {(data ?? []).map((doc) => (
          <Link key={doc.key} href={`/legal/${doc.key}`}>
            <Card className="transition-colors hover:bg-accent/50">
              <CardHeader className="flex-row items-start gap-3">
                <span className="mt-0.5 grid size-8 shrink-0 place-items-center rounded-lg bg-accent text-foreground" aria-hidden="true">
                  <FileText className="size-4" />
                </span>
                <span className="min-w-0">
                  <CardTitle>{doc.title}</CardTitle>
                  <CardDescription className="mt-0.5">
                    Version {doc.version} · effective {doc.effective_date}
                  </CardDescription>
                </span>
              </CardHeader>
              <CardContent className="text-sm text-muted-foreground">{doc.summary}</CardContent>
            </Card>
          </Link>
        ))}
      </div>
    </div>
  );
}
