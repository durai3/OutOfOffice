"use client";

import * as React from "react";
import { AlertTriangle, Gauge } from "lucide-react";
import { api } from "@/lib/api";
import { useApi } from "@/hooks/use-api";
import { PageHeader } from "@/components/page-header";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";

function formatUnits(value: number, unit: string): string {
  if (unit === "seconds") {
    const minutes = value / 60;
    return minutes >= 1 ? `${minutes.toFixed(minutes < 10 ? 1 : 0)} min` : `${Math.round(value)} s`;
  }
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return `${Math.round(value)}`;
}

function barColour(fraction: number): string {
  if (fraction >= 1) return "bg-danger";
  if (fraction >= 0.8) return "bg-warning";
  return "bg-primary";
}

export function UsageView() {
  const usage = useApi(api.usage, { refreshMs: 30_000 });
  const events = useApi(() => api.usageEvents(40), { refreshMs: 60_000 });
  const meetings = useApi(() => api.usageByMeeting(10));

  if (usage.loading && !usage.data) {
    return (
      <div className="grid min-h-[40vh] place-items-center">
        <Spinner />
      </div>
    );
  }
  const data = usage.data;
  if (!data) return <p className="text-sm text-danger">{usage.error}</p>;

  return (
    <div className="space-y-6">
      <PageHeader
        title="Usage"
        description={`${data.plan_describe}. Calls made on your own keys are shown but not counted against this.`}
      />

      {data.blocked ? (
        <div className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/5 p-3 text-sm text-danger">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span>{data.reason}</span>
        </div>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Gauge className="size-4" aria-hidden="true" /> This month
          </CardTitle>
          <CardDescription>
            Since {new Date(data.period_start).toLocaleDateString()} · estimated cost $
            {data.estimated_cost_usd.toFixed(3)} · {data.providers}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {data.lines.map((line) => (
            <div key={line.key}>
              <div className="flex items-baseline justify-between text-xs">
                <span className="font-medium text-foreground">{line.label}</span>
                <span className="text-muted-foreground">
                  {formatUnits(line.used, line.unit)}
                  {line.unlimited ? " (your own keys, no limit)" : ` of ${formatUnits(line.allowed, line.unit)}`}
                </span>
              </div>
              <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-muted">
                <div
                  className={cn("h-full rounded-full transition-all", barColour(line.fraction))}
                  style={{ width: `${Math.min(100, Math.max(line.unlimited ? 0 : line.fraction * 100, line.used > 0 ? 2 : 0))}%` }}
                />
              </div>
            </div>
          ))}
        </CardContent>
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Per meeting</CardTitle>
            <CardDescription>What each recent meeting consumed.</CardDescription>
          </CardHeader>
          <CardContent>
            {meetings.data?.length ? (
              <table className="w-full text-xs">
                <thead className="text-muted-foreground">
                  <tr className="border-b border-border">
                    <th className="pb-1.5 text-left font-medium">Meeting</th>
                    <th className="pb-1.5 text-right font-medium">Tokens</th>
                    <th className="pb-1.5 text-right font-medium">Audio</th>
                    <th className="pb-1.5 text-right font-medium">Spoken</th>
                  </tr>
                </thead>
                <tbody>
                  {meetings.data.map((row) => (
                    <tr key={row.meeting_id} className="border-b border-border/60">
                      <td className="py-1.5 font-mono">{row.meeting_key}</td>
                      <td className="py-1.5 text-right">{formatUnits(row.units.llm ?? 0, "tokens")}</td>
                      <td className="py-1.5 text-right">{formatUnits(row.units.stt ?? 0, "seconds")}</td>
                      <td className="py-1.5 text-right">{formatUnits(row.units.tts ?? 0, "characters")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="text-xs text-muted-foreground">No meetings yet.</p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Recent calls</CardTitle>
            <CardDescription>Every metered request, newest first.</CardDescription>
          </CardHeader>
          <CardContent className="max-h-96 overflow-y-auto">
            {events.data?.length ? (
              <ul className="space-y-1.5 text-xs">
                {events.data.map((event) => (
                  <li key={event.id} className="flex items-baseline justify-between gap-2">
                    <span className="min-w-0 truncate">
                      <span className="font-medium uppercase text-muted-foreground">{event.service}</span>{" "}
                      {event.model || event.provider}
                      {event.own_key ? <span className="ml-1 text-[10px] text-success">your key</span> : null}
                    </span>
                    <span className="shrink-0 font-mono text-muted-foreground">
                      {formatUnits(event.units, event.unit)}
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-xs text-muted-foreground">Nothing metered yet.</p>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
