"use client";

import * as React from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { CheckCircle2, ChevronDown, Clock, FileText, Gavel, HelpCircle, ListChecks, RefreshCw, Search, Send, Users } from "lucide-react";
import { api, errorMessage } from "@/lib/api";
import type { MeetingBrief, MeetingListResponse, PostMeetingReport } from "@/lib/types";
import { cn, formatDateTime, formatMinutes, formatTime, humanTopic } from "@/lib/utils";
import { useApi } from "@/hooks/use-api";
import { useToast } from "@/components/toast";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { PageHeader } from "@/components/page-header";
import { DecisionBadge, TierBadge } from "@/components/status-badge";

export function HistoryView() {
  const router = useRouter();
  const params = useSearchParams();
  const selectedId = params.get("brief");
  const briefs = useApi<MeetingBrief[]>(() => api.history(), { refreshMs: 30_000 });
  const live = useApi<MeetingListResponse>(api.meetings, { refreshMs: 10_000 });
  const [filter, setFilter] = React.useState("");

  const list = React.useMemo(() => {
    const items = briefs.data ?? [];
    const q = filter.trim().toLowerCase();
    if (!q) return items;
    return items.filter((b) => [b.meeting_id, b.executive_summary, ...b.participants, ...b.decisions_made, ...b.action_items.map((a) => `${a.description} ${a.owner}`)].join(" ").toLowerCase().includes(q));
  }, [briefs.data, filter]);

  const selected = selectedId ?? (list.length ? list[0].brief_id : null);

  function select(id: string) {
    router.replace(`/history?brief=${encodeURIComponent(id)}`, { scroll: false });
  }

  return (
    <>
      <PageHeader
        title="Meeting Briefs"
        description="Every concluded meeting: who was there, what was decided, what is owed, and what still needs you."
        actions={
          <Button variant="outline" size="sm" onClick={() => void briefs.refresh()}>
            <RefreshCw className="size-3.5" /> Refresh
          </Button>
        }
      />

      {live.data && live.data.active.length ? <LiveMeetings ids={live.data.active} onConcluded={() => Promise.all([briefs.refresh(), live.refresh()])} /> : null}

      {briefs.error && !briefs.data ? <div className="mb-4 rounded-md border border-danger/40 bg-danger/5 px-4 py-3 text-sm text-danger">{briefs.error}</div> : null}

      <div className="grid gap-4 lg:grid-cols-[minmax(0,22rem)_1fr]">
        <Card className="lg:sticky lg:top-20 lg:self-start">
          <CardHeader className="pb-3">
            <div className="relative">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Search briefs" className="pl-8" aria-label="Search briefs" />
            </div>
          </CardHeader>
          <CardContent className="p-0">
            {!briefs.data ? (
              <div className="flex flex-col gap-2 p-4">
                <Skeleton className="h-14" />
                <Skeleton className="h-14" />
                <Skeleton className="h-14" />
              </div>
            ) : list.length === 0 ? (
              <EmptyState compact icon={<FileText />} title={filter ? "No matches" : "No briefs yet"} description={filter ? "Try a participant, a topic, or an owner." : "When a meeting concludes its executive briefing is archived here."} />
            ) : (
              <ul className="scroll-thin max-h-[70vh] divide-y divide-border overflow-y-auto">
                {list.map((brief) => (
                  <li key={brief.brief_id}>
                    <button
                      type="button"
                      onClick={() => select(brief.brief_id)}
                      aria-current={brief.brief_id === selected ? "true" : undefined}
                      className={cn("w-full px-4 py-3 text-left transition-colors hover:bg-accent/60", brief.brief_id === selected && "bg-accent")}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="truncate font-mono text-xs font-medium">{brief.meeting_id}</span>
                        <span className="shrink-0 text-[11px] text-muted-foreground">{formatDateTime(brief.concluded_at)}</span>
                      </div>
                      <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">{brief.executive_summary}</p>
                      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-muted-foreground">
                        <span className="inline-flex items-center gap-1"><Users className="size-3" /> {brief.participants.length}</span>
                        <span className="inline-flex items-center gap-1"><ListChecks className="size-3" /> {brief.action_items.length}</span>
                        <span className="inline-flex items-center gap-1"><Gavel className="size-3" /> {brief.decisions_made.length}</span>
                        <span className="inline-flex items-center gap-1"><Clock className="size-3" /> {formatMinutes(brief.duration_minutes)}</span>
                      </div>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>

        <div className="min-w-0">
          {selected ? <BriefDetail briefId={selected} /> : briefs.data ? <Card><CardContent className="pt-6"><EmptyState icon={<FileText />} title="Select a brief" /></CardContent></Card> : null}
        </div>
      </div>
    </>
  );
}

function LiveMeetings({ ids, onConcluded }: { ids: string[]; onConcluded: () => Promise<unknown> }) {
  const toast = useToast();
  const [busy, setBusy] = React.useState<string | null>(null);
  return (
    <Card className="mb-4 border-info/30">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Meetings with live memory</CardTitle>
        <CardDescription>Still open in the API process. Conclude one to generate its briefing and archive it here.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-wrap gap-2">
        {ids.map((id) => (
          <div key={id} className="inline-flex items-center gap-2 rounded-md border border-border px-2.5 py-1.5 text-xs">
            <span className="font-mono">{id}</span>
            <Button
              size="sm"
              variant="outline"
              loading={busy === id}
              onClick={async () => {
                setBusy(id);
                try {
                  const report = await api.conclude(id);
                  toast.push({ title: `Concluded ${id}`, description: `Briefing by ${report.summariser}; ${report.receipt.delivered ? "delivered" : "not delivered"} via ${report.receipt.channel}.`, variant: "success" });
                  await onConcluded();
                } catch (err) {
                  toast.push({ title: "Could not conclude", description: errorMessage(err), variant: "error" });
                } finally {
                  setBusy(null);
                }
              }}
            >
              Conclude
            </Button>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

function BriefDetail({ briefId }: { briefId: string }) {
  const report = useApi<PostMeetingReport>(() => api.brief(briefId), { key: briefId });
  const [showTranscript, setShowTranscript] = React.useState(false);
  const data = report.data;

  if (report.error && !data) {
    return (
      <Card>
        <CardContent className="pt-6 text-sm text-danger">{report.error}</CardContent>
      </Card>
    );
  }
  if (!data) {
    return (
      <div className="flex flex-col gap-4">
        <Skeleton className="h-32" />
        <Skeleton className="h-48" />
      </div>
    );
  }

  const snap = data.snapshot;
  const briefing = data.briefing;
  const others = snap.participants;

  return (
    <div className="flex flex-col gap-4 fade-in" key={briefId}>
      <Card>
        <CardHeader className="pb-3">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <CardTitle className="font-mono text-base">{data.meeting_id}</CardTitle>
              <CardDescription>
                {formatDateTime(snap.started_at)} → {formatTime(data.concluded_at)} · {formatMinutes(Math.max(0, (new Date(data.concluded_at).getTime() - new Date(snap.started_at).getTime()) / 60000))}
                {snap.active_topic ? ` · ended on ${humanTopic(snap.active_topic)}` : ""}
              </CardDescription>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={data.receipt.delivered ? "success" : "outline"} dot>
                <Send className="size-3" /> {data.receipt.delivered ? `sent via ${data.receipt.channel}` : "not delivered"}
              </Badge>
              <Badge variant="outline" title={data.fallback_reason ?? undefined}>{data.summariser.split(" ->")[0]}</Badge>
            </div>
          </div>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="flex flex-wrap items-center gap-1.5 text-xs">
            <Users className="size-3.5 text-muted-foreground" />
            {others.length ? others.map((p) => <Badge key={p} variant="secondary">{p}</Badge>) : <span className="text-muted-foreground">no participants recorded</span>}
          </div>
          <div>
            <h3 className="mb-1 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">Executive summary</h3>
            <p className="text-sm leading-relaxed">{briefing.executive_summary}</p>
          </div>
          <dl className="grid grid-cols-2 gap-3 sm:grid-cols-5">
            <Metric label="Utterances" value={snap.stats.utterances} />
            <Metric label="Answered" value={snap.stats.auto_replies} />
            <Metric label="Escalated" value={snap.stats.escalations} />
            <Metric label="Silent" value={snap.stats.silent} />
            <Metric label="Your calls" value={snap.stats.human_decisions} />
          </dl>
          {!data.receipt.delivered && data.receipt.detail ? <p className="text-xs text-muted-foreground">Delivery: {data.receipt.detail}</p> : null}
        </CardContent>
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2"><Gavel className="size-4 text-muted-foreground" /> Decisions made <Badge variant="outline" className="tabular">{briefing.decisions_made.length}</Badge></CardTitle>
          </CardHeader>
          <CardContent>
            {briefing.decisions_made.length === 0 ? <p className="text-xs text-muted-foreground">None recorded.</p> : (
              <ul className="flex flex-col gap-2 text-sm">
                {briefing.decisions_made.map((d, i) => (
                  <li key={i} className="flex gap-2"><CheckCircle2 className="mt-0.5 size-4 shrink-0 text-success" /> <span className="leading-relaxed">{d}</span></li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2"><HelpCircle className="size-4 text-muted-foreground" /> Unresolved questions <Badge variant="outline" className="tabular">{briefing.unresolved_questions.length}</Badge></CardTitle>
          </CardHeader>
          <CardContent>
            {briefing.unresolved_questions.length === 0 ? <p className="text-xs text-muted-foreground">Nothing is waiting on you.</p> : (
              <ul className="flex flex-col gap-2 text-sm">
                {briefing.unresolved_questions.map((q, i) => (
                  <li key={i} className="leading-relaxed">{q}</li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2"><ListChecks className="size-4 text-muted-foreground" /> Action items and owners <Badge variant="outline" className="tabular">{briefing.action_items.length}</Badge></CardTitle>
        </CardHeader>
        <CardContent>
          {briefing.action_items.length === 0 ? <p className="text-xs text-muted-foreground">None recorded.</p> : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-left text-[11px] uppercase tracking-wider text-muted-foreground">
                  <tr className="border-b border-border">
                    <th className="pb-2 pr-3 font-medium">Action</th>
                    <th className="pb-2 pr-3 font-medium">Owner</th>
                    <th className="pb-2 font-medium">Due</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {briefing.action_items.map((a, i) => (
                    <tr key={i}>
                      <td className="py-2 pr-3 leading-relaxed">{a.description}</td>
                      <td className="py-2 pr-3 text-xs">{a.owner}</td>
                      <td className="py-2 text-xs text-muted-foreground">{a.due || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <button type="button" onClick={() => setShowTranscript((v) => !v)} className="flex w-full items-center justify-between text-left" aria-expanded={showTranscript}>
            <CardTitle className="flex items-center gap-2"><FileText className="size-4 text-muted-foreground" /> Transcript <Badge variant="outline" className="tabular">{snap.transcript.length}</Badge></CardTitle>
            <ChevronDown className={cn("size-4 text-muted-foreground transition-transform", showTranscript && "rotate-180")} />
          </button>
        </CardHeader>
        {showTranscript ? (
          <CardContent>
            <ol className="flex flex-col gap-2.5">
              {snap.transcript.map((t) => (
                <li key={t.index} className={cn("flex gap-3 text-sm", t.is_delegate && "rounded-md border border-violet/25 bg-violet/5 p-2")}>
                  <span className="tabular w-20 shrink-0 whitespace-nowrap text-[11px] text-muted-foreground">{formatTime(t.timestamp)}</span>
                  <span className={cn("w-24 shrink-0 truncate text-xs font-semibold", t.is_delegate ? "text-violet" : "text-muted-foreground")}>{t.speaker}</span>
                  <div className="min-w-0 flex-1">
                    <p className="leading-relaxed">{t.text}</p>
                    {!t.is_delegate ? (
                      <div className="mt-1 flex flex-wrap gap-1.5 text-[11px] text-muted-foreground">
                        <DecisionBadge state={t.decision} />
                        <TierBadge tier={t.tier} />
                        {t.topic ? <span>{humanTopic(t.topic)}</span> : null}
                      </div>
                    ) : null}
                  </div>
                </li>
              ))}
            </ol>
          </CardContent>
        ) : null}
      </Card>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-md border border-border bg-muted/40 px-3 py-2">
      <dt className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</dt>
      <dd className="tabular text-base font-semibold">{value}</dd>
    </div>
  );
}
