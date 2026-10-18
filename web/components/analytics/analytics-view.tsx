"use client";

import * as React from "react";
import { AlertTriangle, CheckCircle2, DollarSign, Gavel, HeartPulse, Video } from "lucide-react";
import { api, errorMessage } from "@/lib/api";
import { useApi } from "@/hooks/use-api";
import { PageHeader } from "@/components/page-header";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { HealthTrendChart } from "@/components/analytics/health-trend-chart";
import { formatDateTime } from "@/lib/utils";
import type { HealthAnalyticsResponse, RoiResponse } from "@/lib/types";

const METRIC_LABELS: Record<string, string> = {
  decision_clarity: "Decision clarity",
  participation_score: "Participation balance",
  agenda_adherence: "Agenda adherence",
  action_item_clarity: "Action item clarity",
};

export function AnalyticsView() {
  const health = useApi<HealthAnalyticsResponse>(() => api.analyticsHealth(4));
  const roi = useApi<RoiResponse>(() => api.analyticsRoi(30));

  return (
    <div className="space-y-6">
      <PageHeader title="Analytics" description="How your meetings are going, and what delegating them is worth." />

      <section>
        <h2 className="mb-3 flex items-center gap-1.5 font-mono text-[10.5px] uppercase tracking-[0.12em] text-muted-foreground">
          <DollarSign className="size-3.5" /> ROI · Last 30 Days
        </h2>
        {!roi.data ? (
          <div className="grid gap-4 sm:grid-cols-4"><Skeleton className="h-28" /><Skeleton className="h-28" /><Skeleton className="h-28" /><Skeleton className="h-28" /></div>
        ) : roi.error ? (
          <p className="text-sm text-danger">{errorMessage(roi.error)}</p>
        ) : (
          <div className="grid gap-4 sm:grid-cols-4">
            <RoiTile
              icon={<Video className="size-3.5" />}
              label="Meetings attended"
              value={roi.data.meetings_attended}
              hint="delegated"
            />
            <RoiTile
              icon={<Gavel className="size-3.5" />}
              label="Decisions made"
              value={roi.data.decisions_made}
              hint="recorded"
            />
            <RoiTile
              icon={<CheckCircle2 className="size-3.5" />}
              label="Commitment completion"
              value={`${roi.data.completion_pct.toFixed(0)}%`}
              hint="of resolved action items"
            />
            <RoiTile
              icon={<DollarSign className="size-3.5" />}
              label="Estimated value"
              value={`$${roi.data.estimated_value_usd.toLocaleString(undefined, { maximumFractionDigits: 0 })}`}
              hint={`${roi.data.hours_saved.toFixed(1)}h not spent in meetings, at $150/h`}
            />
          </div>
        )}
      </section>

      <section>
        <h2 className="mb-3 flex items-center gap-1.5 font-mono text-[10.5px] uppercase tracking-[0.12em] text-muted-foreground">
          <HeartPulse className="size-3.5" /> Meeting Health
        </h2>
        {!health.data ? (
          <Skeleton className="h-64" />
        ) : health.error ? (
          <p className="text-sm text-danger">{errorMessage(health.error)}</p>
        ) : (
          <div className="grid gap-4 lg:grid-cols-[1.3fr_1fr]">
            <Card>
              <CardHeader className="pb-3">
                <CardTitle>Weekly health score</CardTitle>
                <CardDescription>Average of decision clarity, participation balance, agenda adherence, and action item clarity.</CardDescription>
              </CardHeader>
              <CardContent>
                <HealthTrendChart points={health.data.weekly_trend} />
              </CardContent>
            </Card>
            <RecentMeetingsCard meetings={health.data.meetings} />
          </div>
        )}
      </section>
    </div>
  );
}

function RoiTile({ icon, label, value, hint }: { icon: React.ReactNode; label: string; value: React.ReactNode; hint: React.ReactNode }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardDescription className="flex items-center gap-1.5 font-mono text-[10.5px] uppercase tracking-[0.12em]">
          {icon} {label}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-1.5">
        <span className="tabular text-[2.25rem] font-semibold leading-none tracking-tight">{value}</span>
        <span className="text-xs text-muted-foreground">{hint}</span>
      </CardContent>
    </Card>
  );
}

function RecentMeetingsCard({ meetings }: { meetings: HealthAnalyticsResponse["meetings"] }) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle>Recent meetings</CardTitle>
        <CardDescription>Scored individually; flags call out what to watch.</CardDescription>
      </CardHeader>
      <CardContent className="p-0">
        {meetings.length === 0 ? (
          <div className="p-5"><EmptyState compact icon={<Video />} title="No scored meetings yet" description="Scores appear once a meeting concludes." /></div>
        ) : (
          <ul className="scroll-thin max-h-[22rem] divide-y divide-border overflow-y-auto">
            {meetings.map((m) => (
              <li key={m.meeting_id || m.concluded_at} className="px-4 py-3">
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate font-mono text-xs font-semibold">{m.meeting_key}</span>
                  <span className="shrink-0 font-mono text-[10.5px] uppercase tracking-[0.1em] tabular text-muted-foreground">{formatDateTime(m.concluded_at)}</span>
                </div>
                <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
                  <span className="tabular inline-flex items-center gap-1 text-sm font-semibold text-foreground">
                    <HeartPulse className="size-3" /> {Math.round(m.health_score * 100)}%
                  </span>
                  {(["decision_clarity", "participation_score", "agenda_adherence", "action_item_clarity"] as const).map((key) => (
                    <span key={key} className="tabular" title={METRIC_LABELS[key]}>{METRIC_LABELS[key].split(" ")[0]} {Math.round(m[key] * 100)}%</span>
                  ))}
                </div>
                {m.flags.length > 0 ? (
                  <ul className="mt-1.5 flex flex-col gap-0.5">
                    {m.flags.map((flag, i) => (
                      <li key={i} className="flex items-start gap-1.5 text-[11px] text-warning">
                        <AlertTriangle className="mt-0.5 size-3 shrink-0" /> {flag}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
