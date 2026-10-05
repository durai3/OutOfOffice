import { Badge, type BadgeVariant } from "@/components/ui/badge";
import type { DecisionState, DelegateActivity, DelegateStatus, PolicyTier } from "@/lib/types";
import { cn } from "@/lib/utils";

export const ACTIVITY_LABEL: Record<DelegateActivity, string> = {
  IDLE: "Idle",
  CONNECTING: "Connecting",
  LISTENING: "Listening",
  THINKING: "Thinking",
  AWAITING_APPROVAL: "Awaiting Telegram approval",
  AWAITING_CONSENT: "Waiting for the room to consent",
  SPEAKING: "Speaking",
  LEAVING: "Leaving",
};

const ACTIVITY_VARIANT: Record<DelegateActivity, BadgeVariant> = {
  IDLE: "secondary",
  CONNECTING: "info",
  LISTENING: "success",
  THINKING: "warning",
  AWAITING_APPROVAL: "danger",
  AWAITING_CONSENT: "warning",
  SPEAKING: "violet",
  LEAVING: "secondary",
};

export const PHASE_LABEL: Record<string, string> = {
  IDLE: "Idle",
  STARTING: "Starting browser",
  LOGGING_IN: "Signing in",
  JOINING: "Joining",
  LOBBY: "Waiting in lobby",
  IN_CALL: "In meeting",
  RECONNECTING: "Reconnecting",
  REMOVED: "Removed from call",
  ENDED: "Call ended",
  LEFT: "Left the call",
  ERROR: "Error",
};

export function ActivityBadge({ activity, className }: { activity: DelegateActivity; className?: string }) {
  const live = activity !== "IDLE";
  return (
    <Badge variant={ACTIVITY_VARIANT[activity]} dot pulse={live && activity !== "LEAVING"} className={className}>
      {ACTIVITY_LABEL[activity]}
    </Badge>
  );
}

export function PhaseBadge({ phase, active, className }: { phase: string; active?: boolean; className?: string }) {
  const variant: BadgeVariant =
    phase === "IN_CALL" ? "success" : phase === "ERROR" ? "danger" : phase === "LOBBY" || phase === "RECONNECTING" ? "warning" : active ? "info" : "outline";
  return (
    <Badge variant={variant} className={className}>
      {PHASE_LABEL[phase] ?? phase}
    </Badge>
  );
}

/** Idle / In-Meeting summary for the dashboard card. */
export function delegateHeadline(status: DelegateStatus | null | undefined): { label: string; tone: "idle" | "live" | "busy" | "error" } {
  if (!status || !status.active) {
    if (status?.exit_code && status.error) return { label: "Idle", tone: "idle" };
    return { label: "Idle", tone: "idle" };
  }
  if (status.phase === "IN_CALL") return { label: "In meeting", tone: "live" };
  if (status.phase === "ERROR") return { label: "Error", tone: "error" };
  return { label: PHASE_LABEL[status.phase] ?? "Connecting", tone: "busy" };
}

const TIER_VARIANT: Record<PolicyTier, BadgeVariant> = { GREEN: "success", YELLOW: "warning", RED: "danger" };

export function TierBadge({ tier, className }: { tier: PolicyTier | null | undefined; className?: string }) {
  if (!tier) return null;
  return (
    <Badge variant={TIER_VARIANT[tier]} className={cn("font-semibold", className)}>
      {tier}
    </Badge>
  );
}

const DECISION_VARIANT: Record<DecisionState, BadgeVariant> = { AUTO_REPLY: "success", ESCALATE: "danger", SILENT: "outline" };
const DECISION_LABEL: Record<DecisionState, string> = { AUTO_REPLY: "Auto reply", ESCALATE: "Escalated", SILENT: "Silent" };

export function DecisionBadge({ state, className }: { state: DecisionState | null | undefined; className?: string }) {
  if (!state) return null;
  return (
    <Badge variant={DECISION_VARIANT[state]} className={className}>
      {DECISION_LABEL[state]}
    </Badge>
  );
}

export function TierDot({ tier, className }: { tier: PolicyTier; className?: string }) {
  const color = tier === "GREEN" ? "bg-success" : tier === "YELLOW" ? "bg-warning" : "bg-danger";
  return <span className={cn("inline-block size-2 rounded-full", color, className)} aria-hidden="true" />;
}
