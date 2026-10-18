"use client";

import * as React from "react";
import { Check, CheckCircle2, Clock3, ListChecks, RefreshCw, X } from "lucide-react";
import { api, errorMessage } from "@/lib/api";
import { useApi } from "@/hooks/use-api";
import { useToast } from "@/components/toast";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { PageHeader } from "@/components/page-header";
import { cn, formatDate } from "@/lib/utils";
import type { CommitmentTrackerRow } from "@/lib/types";

const STATUS_VARIANT: Record<string, "secondary" | "success" | "danger" | "outline"> = {
  probable: "secondary",
  confirmed: "secondary",
  done: "success",
  dropped: "outline",
};

export function CommitmentsTrackerView() {
  const [status, setStatus] = React.useState("open");
  const tracker = useApi<CommitmentTrackerRow[]>(() => api.commitmentsTracker(status), { key: status });
  const toast = useToast();
  const [busyId, setBusyId] = React.useState<string | null>(null);

  async function act(row: CommitmentTrackerRow, action: "done" | "snooze" | "drop") {
    setBusyId(row.id);
    try {
      if (action === "done") await api.markCommitmentDone(row.id);
      else if (action === "snooze") await api.snoozeCommitment(row.id, 1);
      else await api.dropCommitment(row.id);
      await tracker.refresh();
    } catch (err) {
      toast.push({ title: "Could not update", description: errorMessage(err), variant: "error" });
    } finally {
      setBusyId(null);
    }
  }

  const rows = tracker.data ?? [];

  return (
    <>
      <PageHeader
        title="Commitments"
        description="Everything the room has asked your principal to do, across every meeting."
        actions={
          <div className="flex items-center gap-2">
            <Select value={status} onChange={(e) => setStatus(e.target.value)} className="w-36">
              <option value="open">Open</option>
              <option value="done">Done</option>
              <option value="dropped">Dropped</option>
              <option value="">All</option>
            </Select>
            <Button variant="outline" size="sm" onClick={() => void tracker.refresh()}>
              <RefreshCw className="size-3.5" /> Refresh
            </Button>
          </div>
        }
      />

      <Card>
        <CardContent className="p-0">
          {!tracker.data ? (
            <div className="flex flex-col gap-2 p-5">
              <Skeleton className="h-14" />
              <Skeleton className="h-14" />
              <Skeleton className="h-14" />
            </div>
          ) : rows.length === 0 ? (
            <EmptyState icon={<ListChecks />} title="Nothing here" description="Commitments captured in a meeting will show up in this list." />
          ) : (
            <ul className="divide-y divide-border">
              {rows.map((row) => (
                <CommitmentRow key={row.id} row={row} busy={busyId === row.id} onAct={(action) => void act(row, action)} />
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </>
  );
}

function CommitmentRow({
  row,
  busy,
  onAct,
}: {
  row: CommitmentTrackerRow;
  busy: boolean;
  onAct: (action: "done" | "snooze" | "drop") => void;
}) {
  const actionable = row.status === "probable" || row.status === "confirmed";
  const StatusIcon = row.status === "done" ? CheckCircle2 : row.status === "dropped" ? X : row.overdue ? Clock3 : ListChecks;
  const iconTone = row.status === "done" ? "text-success" : row.status === "dropped" ? "text-muted-foreground" : row.overdue ? "text-danger" : "text-muted-foreground";

  return (
    <li className={cn("flex flex-wrap items-start gap-3 px-5 py-3.5", row.overdue && "bg-danger/5")}>
      <div className={cn("mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full border border-border bg-muted/40", iconTone)}>
        <StatusIcon className="size-3.5" />
      </div>

      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-semibold">{row.owner}</span>
          <Badge variant={STATUS_VARIANT[row.status] ?? "secondary"}>{row.status}</Badge>
        </div>
        <p className="mt-1 text-sm leading-relaxed">{row.description}</p>
        <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-muted-foreground">
          {row.meeting_key ? <span className="font-mono">{row.meeting_key}</span> : null}
          <span className={cn("tabular inline-flex items-center gap-1", row.overdue && "font-semibold text-danger")}>
            {row.overdue ? <Clock3 className="size-3" /> : null}
            {row.due_iso ? formatDate(row.due_iso) : row.due_text || "—"}
          </span>
        </div>
      </div>

      {actionable ? (
        <div className="flex shrink-0 items-center gap-1.5">
          <Button size="sm" variant="outline" loading={busy} onClick={() => onAct("done")} title="Mark done">
            <Check className="size-3.5" />
          </Button>
          <Button size="sm" variant="outline" loading={busy} onClick={() => onAct("snooze")} title="Snooze one day">
            <Clock3 className="size-3.5" />
          </Button>
          <Button size="sm" variant="ghost" loading={busy} onClick={() => onAct("drop")} title="Drop">
            <X className="size-3.5" />
          </Button>
        </div>
      ) : null}
    </li>
  );
}
