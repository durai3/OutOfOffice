"use client";

import * as React from "react";
import { FileText, Pencil, RotateCcw, Save, Sparkles, X } from "lucide-react";
import { api, errorMessage } from "@/lib/api";
import { useApi } from "@/hooks/use-api";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { useToast } from "@/components/toast";

/**
 * "What my delegate knows about me", in full, editable.
 *
 * Every personal fact the delegate is allowed to state in a meeting comes from
 * this document. Nothing else about it is guessed: a question whose answer is
 * not here gets an honest "I'll check with you and come back", never an
 * invention. That makes this page the one place a user has to look to know
 * exactly what will be said on their behalf - and the one place to correct it.
 *
 * A hand edit is permanent: rebuilding from documents never silently discards
 * it, because the person being represented outranks the compiler.
 */
export function DossierView() {
  const dossier = useApi(api.dossier);
  const toast = useToast();
  const [draft, setDraft] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState<"save" | "rebuild" | null>(null);

  const editing = draft !== null;
  const data = dossier.data;

  async function save() {
    if (draft === null) return;
    setBusy("save");
    try {
      await api.saveDossier(draft);
      setDraft(null);
      await dossier.refresh();
      toast.push({ title: "Dossier saved", description: "Your edit is now what the delegate will say." });
    } catch (error) {
      toast.push({ title: "Could not save", description: errorMessage(error), variant: "error" });
    } finally {
      setBusy(null);
    }
  }

  async function rebuild() {
    setBusy("rebuild");
    try {
      const next = await api.rebuildDossier(true);
      setDraft(null);
      await dossier.refresh();
      toast.push({
        title: "Dossier rebuilt",
        description: `Recompiled from ${next.sources.length} document${next.sources.length === 1 ? "" : "s"}.`,
      });
    } catch (error) {
      toast.push({ title: "Could not rebuild", description: errorMessage(error), variant: "error" });
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="About Me"
        description="Everything the delegate is allowed to say about you. Anything not written here, it will not claim."
        actions={
          data && !editing ? (
            <div className="flex flex-wrap gap-2">
              <Button variant="outline" size="sm" onClick={() => setDraft(data.markdown)}>
                <Pencil className="size-3.5" /> Edit
              </Button>
              <Button variant="outline" size="sm" onClick={rebuild} disabled={busy !== null}>
                <Sparkles className={busy === "rebuild" ? "size-3.5 animate-pulse" : "size-3.5"} />
                {busy === "rebuild" ? "Rebuilding…" : "Rebuild from documents"}
              </Button>
            </div>
          ) : null
        }
      />

      {dossier.loading && !data ? <Skeleton className="h-96 w-full" /> : null}

      {dossier.error ? (
        <EmptyState
          icon={<FileText />}
          title="Could not load the dossier"
          description={dossier.error}
          action={<Button size="sm" onClick={() => void dossier.refresh()}>Try again</Button>}
        />
      ) : null}

      {data && !data.markdown.trim() ? (
        <EmptyState
          icon={<FileText />}
          title="No dossier compiled yet"
          description="Upload your CV, transcripts and cover letters on the Knowledge page, then compile them into the single document the delegate speaks from."
          action={
            <Button size="sm" onClick={rebuild} disabled={busy !== null}>
              <Sparkles className="size-3.5" />
              {busy === "rebuild" ? "Compiling…" : "Compile from documents"}
            </Button>
          }
        />
      ) : null}

      {data && data.markdown.trim() ? (
        <Card>
          <CardHeader className="flex-row flex-wrap items-center justify-between gap-3">
            <div className="min-w-0">
              <CardTitle className="text-sm">{data.describe}</CardTitle>
              <p className="mt-1 text-xs text-muted-foreground">
                Compiled from {data.sources.length} document{data.sources.length === 1 ? "" : "s"}
                {data.built_by ? ` by ${data.built_by}` : ""}.
              </p>
            </div>
            {data.hand_edited ? (
              <Badge variant="success" title="Rebuilding will not silently discard your edits.">
                Hand-edited
              </Badge>
            ) : null}
          </CardHeader>
          <CardContent>
            {editing ? (
              <div className="space-y-3">
                <Textarea
                  value={draft ?? ""}
                  onChange={(event) => setDraft(event.target.value)}
                  spellCheck={false}
                  className="min-h-[28rem] font-mono text-xs leading-relaxed"
                  aria-label="Dossier markdown"
                />
                <div className="flex flex-wrap items-center gap-2">
                  <Button size="sm" onClick={save} disabled={busy !== null}>
                    <Save className="size-3.5" /> {busy === "save" ? "Saving…" : "Save"}
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => setDraft(null)} disabled={busy !== null}>
                    <X className="size-3.5" /> Cancel
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => setDraft(data.markdown)}
                    disabled={busy !== null || draft === data.markdown}
                  >
                    <RotateCcw className="size-3.5" /> Revert changes
                  </Button>
                  <p className="ml-auto text-xs text-muted-foreground">
                    Saving marks this hand-edited; a rebuild will never overwrite it without asking.
                  </p>
                </div>
              </div>
            ) : (
              <DossierReader markdown={data.markdown} />
            )}
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}

/**
 * Renders the dossier's fixed Markdown shape without pulling in a parser.
 *
 * The compiler only ever emits `# title`, `## Section`, and `- bullet` lines,
 * so a full Markdown dependency would be weight for no benefit.
 */
function DossierReader({ markdown }: { markdown: string }) {
  const blocks = React.useMemo(() => parseDossier(markdown), [markdown]);

  return (
    <div className="space-y-6">
      {blocks.intro ? <p className="text-sm leading-relaxed text-muted-foreground">{blocks.intro}</p> : null}
      <div className="grid gap-5 sm:grid-cols-2">
        {blocks.sections.map((section) => (
          <section key={section.heading} className="min-w-0">
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              {section.heading}
            </h3>
            {section.bullets.length ? (
              <ul className="space-y-1.5">
                {section.bullets.map((bullet, index) => (
                  <li key={index} className="flex gap-2 text-sm leading-relaxed">
                    <span aria-hidden="true" className="mt-2 size-1 shrink-0 rounded-full bg-muted-foreground/50" />
                    <span className="min-w-0 break-words">{bullet}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm italic text-muted-foreground">Not recorded.</p>
            )}
          </section>
        ))}
      </div>
    </div>
  );
}

interface DossierSection {
  heading: string;
  bullets: string[];
}

function parseDossier(markdown: string): { intro: string; sections: DossierSection[] } {
  const sections: DossierSection[] = [];
  const introLines: string[] = [];
  let current: DossierSection | null = null;

  for (const raw of markdown.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    if (line.startsWith("## ")) {
      current = { heading: line.slice(3).trim(), bullets: [] };
      sections.push(current);
    } else if (line.startsWith("# ")) {
      continue;
    } else if (line.startsWith("- ")) {
      current?.bullets.push(line.slice(2).trim());
    } else if (current) {
      if (line.toLowerCase() !== "not recorded.") current.bullets.push(line);
    } else {
      introLines.push(line);
    }
  }
  return { intro: introLines.join(" "), sections };
}
