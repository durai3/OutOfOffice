"use client";

import Link from "next/link";
import { AlertTriangle, ArrowUpRight, BookOpen, Gauge, History, ListChecks, RefreshCw, ShieldCheck, Waves } from "lucide-react";
import { api } from "@/lib/api";
import type { DashboardResponse, DelegateStatus, MeetingBrief, ProfileSummary } from "@/lib/types";
import { cn, formatDateTime, humanTopic, timeAgo } from "@/lib/utils";
import { useApi } from "@/hooks/use-api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHeader } from "@/components/page-header";
import { ActivityBadge, PhaseBadge, TierDot, delegateHeadline } from "@/components/status-badge";
import { DispatchCard } from "@/components/dashboard/dispatch-card";

export function DashboardView() {
  const dash = useApi<DashboardResponse>(api.dashboard, { refreshMs: 5_000 });
  const data = dash.data;

  return (
    <>
      <PageHeader
        title="Overview"
        description="Where the delegate is, which policy governs it, and what it has brought back."
        actions={
          <Button variant="outline" size="sm" onClick={() => void dash.refresh()} loading={dash.loading && Boolean(data)}>
            <RefreshCw className="size-3.5" /> Refresh
          </Button>
        }
      />

      {dash.error ? (
        <div className="mb-4 rounded-md border border-danger/40 bg-danger/5 px-4 py-3 text-sm text-danger">
          {dash.error}
        </div>
      ) : null}

      {data?.quota_blocked ? (
        <div className="mb-4 flex items-start gap-2 rounded-md border border-danger/40 bg-danger/5 px-4 py-3 text-sm text-danger">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span>
            {data.quota_reason}{" "}
            <Link href="/settings" className="underline">Add your own keys</Link> or{" "}
            <Link href="/usage" className="underline">see your usage</Link>.
          </span>
        </div>
      ) : null}

      {(data?.delegates.length ?? 0) > 1 ? (
        <div className="mb-4 rounded-md border border-border bg-card px-4 py-3">
          <p className="text-xs font-medium">{data!.delegates.length} meetings running</p>
          <ul className="mt-1.5 space-y-1 text-xs text-muted-foreground">
            {data!.delegates.map((meeting) => (
              <li key={meeting.meeting_id} className="flex items-center gap-2">
                <ActivityBadge activity={meeting.activity} />
                <span className="font-mono">{meeting.meeting_id}</span>
                <span>{meeting.runtime === "container" ? "in its own container" : "in this server"}</span>
                {meeting.retaining ? null : (
                  <span className="text-warning">· not retaining (consent {meeting.consent_state})</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <DelegateStatusCard status={data?.delegate} loading={!data} />
        <ProfileCard profile={data?.profile} loading={!data} />
        <StatCard
          title="Meetings Attended"
          icon={<History className="size-4" />}
          value={data ? data.meetings_attended : null}
          hint={data ? (data.live_meetings.length ? `${data.live_meetings.length} live in memory` : "archived briefs") : ""}
          href="/history"
        />
        <StatCard
          title="Pending Action Items"
          icon={<ListChecks className="size-4" />}
          value={data ? data.pending_action_items : null}
          hint="across briefs and live meetings"
          href="/history"
        />
      </div>

      <div className="mt-6 grid gap-4 lg:grid-cols-3">
        <div className="lg:col-span-2">
          <DispatchCard status={data?.delegate} onChanged={() => void dash.refresh()} />
        </div>
        <div className="flex flex-col gap-4">
          <SystemCard data={data} />
          <RecentBriefs briefs={data?.recent_briefs} loading={!data} />
        </div>
      </div>
    </>
  );
}

function DelegateStatusCard({ status, loading }: { status: DelegateStatus | undefined; loading: boolean }) {
  const headline = delegateHeadline(status);
  const tone = headline.tone;
  return (
    <Card className="relative overflow-hidden">
      <span
        aria-hidden="true"
        className={cn(
          "absolute inset-x-0 top-0 h-0.5",
          tone === "live" ? "bg-success" : tone === "busy" ? "bg-info" : tone === "error" ? "bg-danger" : "bg-border",
        )}
      />
      <CardHeader className="pb-2">
        <CardDescription className="flex items-center gap-1.5 uppercase tracking-wider">
          <Waves className="size-3.5" /> Active Delegate Status
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        {loading ? (
          <Skeleton className="h-7 w-28" />
        ) : (
          <>
            <div className="flex items-center gap-2">
              <span className="text-2xl font-semibold tracking-tight">{headline.label}</span>
              {status ? <ActivityBadge activity={status.activity} /> : null}
            </div>
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              {status?.active ? (
                <>
                  <PhaseBadge phase={status.phase} active />
                  <span className="font-mono">{status.meeting_code}</span>
                  {status.active_topic ? <span>· topic {humanTopic(status.active_topic)}</span> : null}
                </>
              ) : status?.finished_at ? (
                <span>
                  Last session {status.meeting_code} {timeAgo(status.finished_at)}
                  {status.error ? <span className="text-danger"> · {status.error.length > 60 ? `${status.error.slice(0, 60)}…` : status.error}</span> : null}
                </span>
              ) : (
                <span>No session yet. Dispatch one below.</span>
              )}
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}

function ProfileCard({ profile, loading }: { profile: ProfileSummary | undefined; loading: boolean }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardDescription className="flex items-center gap-1.5 uppercase tracking-wider">
          <ShieldCheck className="size-3.5" /> Active Policy Profile
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        {loading || !profile ? (
          <Skeleton className="h-7 w-40" />
        ) : (
          <>
            <div className="flex items-baseline justify-between gap-2">
              <span className="truncate text-2xl font-semibold tracking-tight">{profile.display_name}</span>
              <Link href="/policy" className="inline-flex shrink-0 items-center gap-0.5 text-xs text-muted-foreground hover:text-foreground">
                edit <ArrowUpRight className="size-3" />
              </Link>
            </div>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
              {(["GREEN", "YELLOW", "RED"] as const).map((tier) => (
                <span key={tier} className="inline-flex items-center gap-1.5">
                  <TierDot tier={tier} /> <span className="tabular font-medium text-foreground">{profile.rules_by_tier[tier]}</span> {tier.toLowerCase()}
                </span>
              ))}
              <span>· {profile.knowledge_entries} facts</span>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}

function StatCard({ title, icon, value, hint, href }: { title: string; icon: React.ReactNode; value: number | null; hint: string; href: string }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardDescription className="flex items-center gap-1.5 uppercase tracking-wider">
          {icon} {title}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        {value === null ? (
          <Skeleton className="h-7 w-16" />
        ) : (
          <div className="flex items-baseline justify-between gap-2">
            <span className="tabular text-2xl font-semibold tracking-tight">{value}</span>
            <Link href={href} className="inline-flex items-center gap-0.5 text-xs text-muted-foreground hover:text-foreground">
              view <ArrowUpRight className="size-3" />
            </Link>
          </div>
        )}
        <span className="text-xs text-muted-foreground">{hint}</span>
      </CardContent>
    </Card>
  );
}

function SystemCard({ data }: { data: DashboardResponse | undefined }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Your setup</CardTitle>
        <CardDescription>What your delegate runs on. Nothing here is shared with another account.</CardDescription>
      </CardHeader>
      <CardContent>
        {!data ? (
          <div className="flex flex-col gap-2">
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-5/6" />
            <Skeleton className="h-4 w-2/3" />
          </div>
        ) : (
          <dl className="flex flex-col gap-2.5 text-xs">
            <SystemRow label="Escalation" value={data.escalation_bridge} ok={!data.escalation_bridge.startsWith("disabled")} />
            <SystemRow label="Knowledge" value={data.knowledge} ok={data.knowledge_available} />
            <SystemRow label="Briefing" value={data.summariser} ok={!data.summariser.includes("disabled")} />
            <SystemRow
              label="Consent"
              value={CONSENT_LABEL[data.consent_mode] ?? data.consent_mode}
              ok={data.consent_mode !== "notify_only"}
            />
            <SystemRow
              label="Plan"
              value={`${data.plan} · ${Math.round(Math.max(0, ...Object.values(data.usage_fractions)) * 100)}% of this month used`}
              ok={!data.quota_blocked}
            />
            <SystemRow label="Version" value={`API ${data.version}`} ok />
          </dl>
        )}
      </CardContent>
    </Card>
  );
}

const CONSENT_LABEL: Record<string, string> = {
  notify_only: "announced to the room",
  all_party: "waits for the room to agree",
  single_party: "single-party basis, announced",
};

function SystemRow({ label, value, ok }: { label: string; value: string; ok: boolean }) {
  return (
    <div className="flex items-start gap-2">
      <span className={cn("mt-1.5 size-1.5 shrink-0 rounded-full", ok ? "bg-success" : "bg-warning")} aria-hidden="true" />
      <dt className="w-20 shrink-0 text-muted-foreground">{label}</dt>
      <dd className="min-w-0 flex-1 break-words leading-5" title={value}>{value}</dd>
    </div>
  );
}

function RecentBriefs({ briefs, loading }: { briefs: MeetingBrief[] | undefined; loading: boolean }) {
  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <div>
          <CardTitle>Recent briefs</CardTitle>
          <CardDescription>Latest executive summaries.</CardDescription>
        </div>
        <Link href="/history" className="text-xs text-muted-foreground hover:text-foreground">all</Link>
      </CardHeader>
      <CardContent>
        {loading ? (
          <div className="flex flex-col gap-2">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
          </div>
        ) : !briefs || briefs.length === 0 ? (
          <EmptyState compact icon={<BookOpen />} title="No briefs yet" description="Concluded meetings appear here with their summaries and action items." />
        ) : (
          <ul className="flex flex-col divide-y divide-border">
            {briefs.map((brief) => (
              <li key={brief.brief_id} className="py-2.5 first:pt-0 last:pb-0">
                <Link href={`/history?brief=${encodeURIComponent(brief.brief_id)}`} className="group block">
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate font-mono text-xs">{brief.meeting_id}</span>
                    <span className="shrink-0 text-[11px] text-muted-foreground">{formatDateTime(brief.concluded_at)}</span>
                  </div>
                  <p className="mt-0.5 line-clamp-2 text-xs text-muted-foreground group-hover:text-foreground">{brief.executive_summary}</p>
                  <p className="mt-1 text-[11px] text-muted-foreground">
                    {brief.action_items.length} action{brief.action_items.length === 1 ? "" : "s"} · {brief.decisions_made.length} decision{brief.decisions_made.length === 1 ? "" : "s"}
                  </p>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
