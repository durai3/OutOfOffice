"use client";

import * as React from "react";
import Link from "next/link";
import {
  CalendarClock,
  Check,
  CheckCircle2,
  ClipboardCopy,
  Download,
  ExternalLink,
  FileText,
  Gavel,
  HelpCircle,
  ListChecks,
  Smartphone,
  Users,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/toast";
import { api, ApiError, errorMessage } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { PlaygroundSession } from "@/lib/types";

/**
 * The post-meeting briefing, laid out like a document rather than a log.
 *
 * Everything in it comes from the real report (`session.report`): the summary
 * and decisions are Gemini's, the action items are the commitment ledger's, and
 * "what you decided" is the approvals you made from the phone.
 */

const OUTCOME_LABEL: Record<string, string> = {
  APPROVED: "Approved",
  REJECTED: "Rejected",
  DECLINED_POLITELY: "Declined politely",
};

function pretty(value: string): string {
  const text = value.replaceAll("_", " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function minutesBetween(from: string, to: string): number {
  const ms = new Date(to).getTime() - new Date(from).getTime();
  return Number.isFinite(ms) ? Math.max(1, Math.round(ms / 60000)) : 1;
}

interface BriefingData {
  title: string;
  when: string;
  minutes: number;
  participants: string[];
  summary: string;
  decisions: string[];
  actions: { description: string; owner: string; due: string }[];
  open: string[];
  yours: { topic: string; heard: string; outcome: string }[];
  principal: string;
}

function collect(session: PlaygroundSession): BriefingData | null {
  const report = session.report;
  if (!report) return null;
  const briefing = report.briefing;
  const concluded = report.concluded_at;
  return {
    title: `Meeting briefing - ${session.principal}`,
    when: new Date(concluded).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }),
    minutes: minutesBetween(session.started_at, concluded),
    participants: session.participants,
    summary: briefing.executive_summary,
    decisions: briefing.decisions_made,
    actions: briefing.action_items.map((a) => ({ description: a.description, owner: a.owner, due: a.due })),
    open: briefing.unresolved_questions,
    yours: session.approvals
      .filter((a) => !a.pending)
      .map((a) => ({
        topic: pretty(a.topic ?? "request"),
        heard: a.heard,
        outcome: OUTCOME_LABEL[a.outcome ?? ""] ?? a.outcome ?? "Answered",
      })),
    principal: session.principal,
  };
}

const esc = (value: string) =>
  value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");

/** Clean semantic HTML: pastes into Google Docs and Word with headings, lists and a table intact. */
function toHtml(d: BriefingData): string {
  const list = (items: string[]) => (items.length ? `<ul>${items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>` : "<p><i>None.</i></p>");
  const actions = d.actions.length
    ? `<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse"><tr><th align="left">Action item</th><th align="left">Owner</th><th align="left">Due</th></tr>${d.actions
        .map((a) => `<tr><td>${esc(a.description)}</td><td>${esc(a.owner)}</td><td>${esc(a.due || "-")}</td></tr>`)
        .join("")}</table>`
    : "<p><i>No action items.</i></p>";
  const yours = d.yours.length
    ? `<ul>${d.yours.map((y) => `<li><b>${esc(y.topic)}</b>: ${esc(y.outcome)} (&ldquo;${esc(y.heard)}&rdquo;)</li>`).join("")}</ul>`
    : "<p><i>Nothing needed your approval.</i></p>";
  return `<h1>${esc(d.title)}</h1>
<p>${esc(d.when)} &middot; ${d.minutes} min &middot; With ${esc(d.participants.join(", ") || "no one else")}</p>
<h2>Summary</h2><p>${esc(d.summary)}</p>
<h2>Decisions</h2>${list(d.decisions)}
<h2>Action items</h2>${actions}
<h2>Left open</h2>${list(d.open)}
<h2>What you decided</h2>${yours}
<p><i>Written by Out of Office for ${esc(d.principal)}.</i></p>`;
}

function toText(d: BriefingData): string {
  const bullets = (items: string[]) => (items.length ? items.map((i) => `- ${i}`).join("\n") : "- None");
  return [
    d.title,
    `${d.when} - ${d.minutes} min - With ${d.participants.join(", ") || "no one else"}`,
    "",
    "SUMMARY",
    d.summary,
    "",
    "DECISIONS",
    bullets(d.decisions),
    "",
    "ACTION ITEMS",
    bullets(d.actions.map((a) => `${a.description} (${a.owner}${a.due ? `, due ${a.due}` : ""})`)),
    "",
    "LEFT OPEN",
    bullets(d.open),
    "",
    "WHAT YOU DECIDED",
    bullets(d.yours.map((y) => `${y.topic}: ${y.outcome}`)),
  ].join("\n");
}

export function BriefingCard({ session, model }: { session: PlaygroundSession; model: string | null }) {
  const toast = useToast();
  const data = React.useMemo(() => collect(session), [session]);
  const [docBusy, setDocBusy] = React.useState(false);
  const [pasteUrl, setPasteUrl] = React.useState<string | null>(null);
  if (!data) return null;

  async function copy(html: boolean): Promise<boolean> {
    if (!data) return false;
    try {
      if (html && typeof ClipboardItem !== "undefined") {
        await navigator.clipboard.write([
          new ClipboardItem({
            "text/html": new Blob([toHtml(data)], { type: "text/html" }),
            "text/plain": new Blob([toText(data)], { type: "text/plain" }),
          }),
        ]);
      } else {
        await navigator.clipboard.writeText(toText(data));
      }
      return true;
    } catch {
      return false;
    }
  }

  async function openInDocs() {
    if (!data) return;
    setDocBusy(true);
    // Open the tab inside the click, or the popup blocker eats it after the request below.
    const tab = window.open("about:blank", "_blank");
    try {
      const { url } = await api.playgroundBriefingDoc();
      if (tab) tab.location.href = url;
      else window.open(url, "_blank");
      toast.push({ title: "Opened in Google Docs", description: "A view-only copy is shared by link. Use File → Make a copy to edit it." });
      return;
    } catch (error) {
      // 501 = the operator has not set up the exporter. Anything else: fall through to paste too.
      const notConfigured = error instanceof ApiError && error.status === 501;
      if (!notConfigured) {
        toast.push({ title: "Could not create the doc automatically", description: errorMessage(error), variant: "error" });
      }
    } finally {
      setDocBusy(false);
    }
    // Fallback: a blank doc cannot be pre-filled by link, so put the briefing on the clipboard.
    // Copy BEFORE the new tab takes focus - the clipboard API refuses to write from a page that is not focused.
    tab?.close();
    const ok = await copy(true);
    const url = `https://docs.google.com/document/create?title=${encodeURIComponent(data.title)}`;
    if (!window.open(url, "_blank")) setPasteUrl(url);
    toast.push({
      title: ok ? "Briefing copied - paste it into the blank doc" : "Could not copy the briefing",
      description: ok ? "Click into the new doc and press ⌘V (Ctrl+V). Headings, lists and the table keep their formatting." : "Use the Copy button, then paste it into the doc.",
      variant: ok ? "success" : "error",
    });
  }

  async function copyOnly() {
    const ok = await copy(true);
    toast.push({ title: ok ? "Briefing copied" : "Could not copy", variant: ok ? "success" : "error" });
  }

  function download() {
    if (!data) return;
    const page = `<!doctype html><html><head><meta charset="utf-8"><title>${esc(data.title)}</title></head><body>${toHtml(data)}</body></html>`;
    const url = URL.createObjectURL(new Blob([page], { type: "application/msword" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `briefing-${new Date().toISOString().slice(0, 10)}.doc`;
    link.click();
    URL.revokeObjectURL(url);
  }

  return (
    <article className="fade-in overflow-hidden rounded-xl border border-primary/30 bg-card shadow-sm">
      <header className="space-y-4 border-b border-border bg-primary/5 px-6 py-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="space-y-1">
            <p className="flex items-center gap-1.5 font-mono text-[10.5px] uppercase tracking-[0.12em] text-primary">
              <FileText className="size-3" aria-hidden="true" /> Meeting briefing
            </p>
            <h2 className="text-xl font-semibold tracking-tight">{data.title.replace("Meeting briefing - ", "")}&apos;s delegate covered this meeting</h2>
            <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
              <span className="inline-flex items-center gap-1"><CalendarClock className="size-3" aria-hidden="true" /> {data.when} · {data.minutes} min</span>
              <span className="inline-flex items-center gap-1"><Users className="size-3" aria-hidden="true" /> {data.participants.join(", ") || "No one else"}</span>
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" onClick={openInDocs} loading={docBusy}>
              <ExternalLink className="size-3.5" /> Open in Google Docs
            </Button>
            <Button size="sm" variant="outline" onClick={copyOnly}>
              <ClipboardCopy className="size-3.5" /> Copy
            </Button>
            <Button size="sm" variant="outline" onClick={download}>
              <Download className="size-3.5" /> Download
            </Button>
          </div>
        </div>
        {pasteUrl ? (
          <p className="rounded-lg border border-border bg-card px-3 py-2 text-xs text-muted-foreground">
            Your browser blocked the new tab. The briefing is on your clipboard:{" "}
            <a href={pasteUrl} target="_blank" rel="noreferrer" className="font-medium text-foreground underline underline-offset-2">
              open a blank Google Doc
            </a>{" "}
            and paste.
          </p>
        ) : null}
        <ul className="grid grid-cols-3 gap-2 text-center">
          <Stat label="Decisions" value={data.decisions.length} />
          <Stat label="Action items" value={data.actions.length} />
          <Stat label="Left open" value={data.open.length} />
        </ul>
      </header>

      <div className="space-y-6 px-6 py-6">
        <section aria-labelledby="brief-summary" className="space-y-2">
          <h3 id="brief-summary" className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">Summary</h3>
          <p className="border-l-2 border-primary pl-4 text-[15px] leading-relaxed">{data.summary}</p>
        </section>

        {data.yours.length ? (
          <Section icon={<Smartphone className="size-4" />} title="What you decided from your phone">
            <ul className="space-y-2">
              {data.yours.map((y, i) => (
                <li key={`${y.topic}-${i}`} className="flex items-start justify-between gap-3 rounded-lg border border-border bg-muted/40 px-3.5 py-2.5">
                  <div className="min-w-0">
                    <p className="text-sm font-medium">{y.topic}</p>
                    <p className="truncate text-xs text-muted-foreground">&ldquo;{y.heard}&rdquo;</p>
                  </div>
                  <Badge variant={y.outcome === "Approved" ? "success" : "secondary"} dot>{y.outcome}</Badge>
                </li>
              ))}
            </ul>
          </Section>
        ) : null}

        <div className="grid gap-6 md:grid-cols-2">
          <Section icon={<Gavel className="size-4" />} title="Decisions">
            <Bullets items={data.decisions} empty="No decisions were made." />
          </Section>
          <Section icon={<HelpCircle className="size-4" />} title="Left open">
            <Bullets items={data.open} empty="Nothing left hanging." />
          </Section>
        </div>

        <Section icon={<ListChecks className="size-4" />} title="Action items">
          {data.actions.length ? (
            <div className="overflow-hidden rounded-lg border border-border">
              <table className="w-full text-sm">
                <thead className="bg-muted/60 text-left text-[11px] uppercase tracking-wider text-muted-foreground">
                  <tr>
                    <th className="px-3.5 py-2 font-semibold">Item</th>
                    <th className="px-3.5 py-2 font-semibold">Owner</th>
                    <th className="px-3.5 py-2 font-semibold">Due</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {data.actions.map((a, i) => (
                    <tr key={`${a.description}-${i}`}>
                      <td className="px-3.5 py-2.5">{a.description}</td>
                      <td className="px-3.5 py-2.5 text-muted-foreground">{a.owner}</td>
                      <td className="px-3.5 py-2.5">{a.due ? <Badge variant="outline">{a.due}</Badge> : <span className="text-muted-foreground">-</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">No action items were handed to the delegate.</p>
          )}
        </Section>
      </div>

      <footer className="flex flex-wrap items-center justify-between gap-2 border-t border-border bg-muted/30 px-6 py-3 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <CheckCircle2 className="size-3.5 text-success" aria-hidden="true" />
          Sent to your phone{model ? ` · written by ${model}` : ""}
        </span>
        <span>
          Saved under{" "}
          <Link href="/history" className="underline underline-offset-2">Briefs</Link> and{" "}
          <Link href="/commitments" className="underline underline-offset-2">Commitments</Link>
        </span>
      </footer>
    </article>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <li className="rounded-lg border border-border bg-card px-3 py-2">
      <p className="text-xl font-semibold leading-none tabular-nums">{value}</p>
      <p className="mt-1 text-[11px] text-muted-foreground">{label}</p>
    </li>
  );
}

function Section({ icon, title, children }: { icon: React.ReactNode; title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-2.5">
      <h3 className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
        <span className="text-primary">{icon}</span> {title}
      </h3>
      {children}
    </section>
  );
}

function Bullets({ items, empty }: { items: string[]; empty: string }) {
  if (!items.length) return <p className="text-sm text-muted-foreground">{empty}</p>;
  return (
    <ul className="space-y-2">
      {items.map((item) => (
        <li key={item} className="flex gap-2.5 text-sm leading-relaxed">
          <Check className={cn("mt-1 size-3.5 shrink-0 text-primary")} aria-hidden="true" />
          <span>{item}</span>
        </li>
      ))}
    </ul>
  );
}
