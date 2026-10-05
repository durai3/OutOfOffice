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
      <h1 className="text-xl font-semibold tracking-tight">Legal</h1>
      <p className="mt-1 text-sm text-muted-foreground">
        What DELEG8 does with meetings it joins, and what you take responsibility for by sending it.
      </p>
      {loading ? <Spinner className="mt-6" /> : null}
      <div className="mt-6 grid gap-3">
        {(data ?? []).map((doc) => (
          <Link key={doc.key} href={`/legal/${doc.key}`}>
            <Card className="transition-colors hover:bg-accent/50">
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <FileText className="size-4" aria-hidden="true" /> {doc.title}
                </CardTitle>
                <CardDescription>
                  Version {doc.version} · effective {doc.effective_date}
                </CardDescription>
              </CardHeader>
              <CardContent className="text-sm text-muted-foreground">{doc.summary}</CardContent>
            </Card>
          </Link>
        ))}
      </div>
    </div>
  );
}
