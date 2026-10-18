"use client";

import * as React from "react";
import { Sparkles, Upload } from "lucide-react";
import { api, errorMessage } from "@/lib/api";
import { useApi } from "@/hooks/use-api";
import { Button } from "@/components/ui/button";

export function DocumentsStep({ onDone }: { onDone: () => Promise<void> | void }) {
  const knowledge = useApi(api.knowledge);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const input = React.useRef<HTMLInputElement>(null);

  async function upload(files: FileList | null) {
    if (!files?.length) return;
    setBusy(true);
    setError(null);
    try {
      const report = await api.upload(Array.from(files));
      const failed = report.files.filter((f) => f.status === "failed");
      if (failed.length) setError(failed.map((f) => `${f.source}: ${f.detail}`).join("; "));
      await knowledge.refresh();
      await onDone();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
      if (input.current) input.current.value = "";
    }
  }

  async function loadSample() {
    setBusy(true);
    setError(null);
    try {
      await api.playgroundSampleData();
      await knowledge.refresh();
      await onDone();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const documents = knowledge.data?.documents ?? [];

  return (
    <div className="space-y-3 text-sm">
      <p className="text-muted-foreground">
        A CV, a project brief, notes - anything your delegate should be able to speak from. We turn them into a
        short profile you can read and correct, and only your account can see them.
      </p>
      <input
        ref={input}
        type="file"
        multiple
        accept=".md,.txt,.pdf"
        className="hidden"
        onChange={(e) => void upload(e.target.files)}
      />
      <div className="flex flex-wrap items-center gap-2">
        <Button onClick={() => input.current?.click()} loading={busy}>
          <Upload className="size-4" /> Choose files
        </Button>
        <Button variant="outline" onClick={() => void loadSample()} disabled={busy}>
          <Sparkles className="size-4" /> Use sample data
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        Just exploring? Sample data gives your delegate a fictional profile, project and team page to speak from.
        It takes about ten seconds, and you can delete it any time from the Knowledge page.
      </p>
      {documents.length ? (
        <ul className="space-y-1 rounded-lg border border-border p-3 text-xs text-muted-foreground">
          {documents.map((doc) => (
            <li key={doc.source}>
              {doc.source} — {doc.chunks} chunk{doc.chunks === 1 ? "" : "s"}
            </li>
          ))}
        </ul>
      ) : null}
      {error ? <p className="text-xs text-danger">{error}</p> : null}
    </div>
  );
}
