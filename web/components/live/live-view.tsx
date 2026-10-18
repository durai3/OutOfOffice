"use client";

import * as React from "react";
import Link from "next/link";
import {
  AlertTriangle,
  ArrowDownToLine,
  Bot,
  Brain,
  CheckCircle2,
  Clock,
  Ear,
  ExternalLink,
  Gavel,
  HelpCircle,
  ListChecks,
  MessageSquareWarning,
  Mic,
  MicOff,
  OctagonX,
  Plug,
  PlugZap,
  Send,
  ShieldAlert,
  Volume2,
} from "lucide-react";
import { api, errorMessage } from "@/lib/api";
import type { ActionItem, AgreedDecision, DelegateActivity, DelegateStatus, OpenQuestion } from "@/lib/types";
import { cn, formatDuration, formatTime, humanTopic } from "@/lib/utils";
import { type EscalationEntry, type LiveState, type TimelineEntry, type TranscriptLine, useLiveFeed } from "@/hooks/use-live-feed";
import { useToast } from "@/components/toast";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHeader } from "@/components/page-header";
import { ACTIVITY_LABEL, DecisionBadge, PhaseBadge, TierBadge } from "@/components/status-badge";

/**
 * The delegate's hearing, when there is something to say about it.
 *
 * Silence in the transcript has two completely different causes - a quiet room
 * and an input device that carries nothing - and they used to look identical
 * here. The backend measures the input signal, so this says which one it is,
 * and what to do about the broken case.
 */
// Peaks are 16-bit ints; this maps the real reading onto a 0-1 scale the same
// way InputLevelMeter does (log, not linear), just stretched across more bars.
function peakFill(peak: number): number {
  if (!peak || peak <= 0) return 0;
  return Math.min(1, Math.max(0, Math.log10(peak) / 4.5));
}

/**
 * The input-level history as a short waveform, promoted to its own panel -
 * the same real peak reading InputLevelMeter uses, just buffered over time
 * instead of a single 5-bar snapshot. Falls back to the error panel when the
 * backend reports a specific audio problem.
 */
function AudioPanel({ audio, listening, history }: { audio: LiveState["audio"]; listening: boolean; history: number[] }) {
  if (audio.problem) {
    return (
      <div className="flex gap-2.5 rounded-xl border border-danger/40 bg-danger/10 px-4 py-3 text-xs text-danger">
        <MicOff className="mt-0.5 size-4 shrink-0" />
        <div className="space-y-1">
          <p className="font-medium">The delegate cannot hear the meeting</p>
          <p className="text-danger/90">{audio.problem}</p>
          <p className="text-danger/80">
            Run <code className="rounded bg-danger/15 px-1">python -m src.audio.cable_check</code> to measure every
            virtual cable and see which one works.
          </p>
        </div>
      </div>
    );
  }
  if (!listening || !audio.inputDevice) return null;

  const last = history[history.length - 1] ?? 0;
  const lastFill = peakFill(last);
  const tone = lastFill > 0.88 ? "text-danger" : lastFill > 0.68 ? "text-warning" : "text-success";

  return (
    <div className="rounded-xl border border-white/10 bg-white/[0.035] px-4 py-3.5">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2.5">
          <span className="font-mono text-[10.5px] tracking-[0.12em] text-white/45">INPUT LEVEL</span>
          <span className="inline-flex items-center gap-1.5 rounded-full bg-white/[0.07] px-2.5 py-1 text-[11.5px] text-white/75">
            <Mic className="size-3.5" />
            {audio.muted
              ? "input muted while the delegate speaks"
              : audio.heardAnything
                ? `hearing the room on ${audio.inputDevice}`
                : `listening on ${audio.inputDevice} · no sound yet`}
          </span>
          {audio.inAppMic ? (
            <Badge variant="outline" className="h-5 text-[10px]" title={`The meeting app selects ${audio.inAppMic} as its microphone`}>
              in-app mic
            </Badge>
          ) : null}
        </div>
        <span className={cn("font-mono text-[13px] font-medium", tone)}>peak {last}</span>
      </div>
      <div className="flex h-[52px] items-end gap-[3px]">
        {history.map((value, index) => {
          const fill = peakFill(value);
          const height = Math.round(4 + fill * 48);
          const color = fill > 0.88 ? "bg-danger" : fill > 0.68 ? "bg-warning" : "bg-success";
          const opacity = 0.35 + 0.65 * (index / Math.max(1, history.length - 1));
          return <div key={index} className={cn("min-w-[2px] flex-1 rounded-sm", color)} style={{ height, opacity }} />;
        })}
      </div>
      <div className="mt-1.5 flex justify-between font-mono text-[10px] text-white/35">
        <span>quiet</span>
        <span>loud</span>
      </div>
    </div>
  );
}

function useNow(intervalMs = 1000): number {
  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}

const PEAK_HISTORY_SIZE = 56;

/**
 * A rolling window of real peak readings, so the meter can draw a short
 * waveform history instead of one instantaneous bar. The backend only ever
 * reports the current peak, not a timeseries - this buffers it client-side.
 */
function usePeakHistory(peak: number | null, size = PEAK_HISTORY_SIZE): number[] {
  const [history, setHistory] = React.useState<number[]>(() => Array.from({ length: size }, () => 0));
  const seen = React.useRef<number | null>(null);
  React.useEffect(() => {
    if (peak === seen.current) return;
    seen.current = peak;
    setHistory((prev) => [...prev.slice(1), peak ?? 0]);
  }, [peak]);
  return history;
}

export function LiveView() {
  const feed = useLiveFeed();
  const status = feed.status;
  const active = Boolean(status?.active);
  const hasContent = active || feed.turns.length > 0;
  const peakHistory = usePeakHistory(feed.audio.peak);
  const [confirm, setConfirm] = React.useState(false);
  const [recalling, setRecalling] = React.useState(false);
  const toast = useToast();

  async function recall() {
    setRecalling(true);
    try {
      await api.abort("Emergency recall from the Live Monitor");
      toast.push({ title: "Emergency recall issued", description: "Speech cut, leaving the call, briefing on its way.", variant: "success" });
      setConfirm(false);
    } catch (err) {
      toast.push({ title: "Recall failed", description: errorMessage(err), variant: "error" });
    } finally {
      setRecalling(false);
    }
  }

  return (
    <>
      <PageHeader
        title="Live Monitor"
        description="Real-time view of the delegate in the call: what it hears, decides, says, and escalates."
        actions={
          <>
            <ConnectionBadge state={feed.connection} onReconnect={feed.reconnect} />
            <Button variant="destructive" onClick={() => setConfirm(true)} disabled={!active} title={active ? "Drop the bot from the call now" : "No delegate is in a call"}>
              <OctagonX className="size-4" /> Emergency Recall / Mute
            </Button>
          </>
        }
      />

      <div className="dark relative mt-2 overflow-hidden rounded-2xl border border-white/10 bg-[linear-gradient(160deg,#17161c_0%,#0e0e10_65%)] p-4 shadow-[0_28px_60px_-28px_rgba(0,0,0,0.7)] sm:p-5">
        {hasContent ? (
          <div className="flex flex-col gap-4">
            <StatusStrip status={status} connection={feed.connection} />
            <ConsentStrip status={status} />
            <AudioPanel audio={feed.audio} listening={Boolean(status?.active)} history={peakHistory} />
            <div className="grid gap-4 xl:grid-cols-3">
              <div className="flex flex-col gap-4 xl:col-span-2">
                <NowPanel status={status} state={feed} />
                <Transcript state={feed} />
              </div>
              <div className="flex flex-col gap-4">
                <Escalations entries={feed.escalations} pending={status?.pending_escalations ?? 0} />
                <ActionItems items={feed.actions} />
                <Decisions items={feed.decisions} />
                <OpenQuestions items={feed.questions} />
                <Timeline entries={feed.timeline} />
                {feed.lastReport ? <LastBriefing state={feed} /> : null}
              </div>
            </div>
          </div>
        ) : (
          <div className="flex flex-col items-center gap-3 py-16 text-center">
            <div className="relative mb-2 size-[160px]" aria-hidden="true">
              <div className="absolute inset-0 rounded-full border border-white/10" />
              <div className="absolute inset-[26px] rounded-full border border-white/10" />
              <div className="absolute inset-[52px] rounded-full border border-white/10" />
              <div className="absolute left-1/2 top-1/2 h-px w-20 origin-left animate-[d8spin_4s_linear_infinite] bg-[linear-gradient(to_right,#d8d8de,transparent)]" />
              <div className="absolute left-1/2 top-1/2 size-2 -translate-x-1/2 -translate-y-1/2 rounded-full bg-[#d8d8de]" />
            </div>
            <p className="text-lg font-medium text-white">The delegate is idle</p>
            <p className="max-w-md text-sm text-white/50">
              {status?.error
                ? `The last session ended with an error: ${status.error}`
                : "Dispatch it to a Google Meet from the Overview page and this screen fills in as soon as the browser starts."}
            </p>
            <Link href="/" className={cn(buttonVariants({ variant: "outline", size: "sm" }), "mt-2 border-white/15 bg-transparent text-white hover:bg-white/10")}>
              Go to Overview
            </Link>
            {feed.lastReport ? <LastBriefing state={feed} /> : null}
            {feed.timeline.length > 0 ? <Timeline entries={feed.timeline} className="mt-2 w-full text-left" /> : null}
          </div>
        )}
      </div>

      <Dialog
        open={confirm}
        onClose={() => setConfirm(false)}
        title="Emergency recall"
        description="The delegate stops speaking immediately, clicks Leave call, closes the browser, and sends the executive briefing to Telegram."
        footer={
          <>
            <Button variant="outline" onClick={() => setConfirm(false)}>Cancel</Button>
            <Button variant="destructive" onClick={recall} loading={recalling}>
              <OctagonX className="size-4" /> Drop the bot now
            </Button>
          </>
        }
      >
        <p className="text-sm text-muted-foreground">
          Meeting <span className="font-mono text-foreground">{status?.meeting_code ?? "—"}</span>
          {status?.activity === "SPEAKING" ? " · the delegate is mid-sentence and will be cut off." : status?.activity === "AWAITING_APPROVAL" ? " · a Telegram escalation is pending; it resolves as a timeout." : "."}
        </p>
      </Dialog>
    </>
  );
}

function ConnectionBadge({ state, onReconnect }: { state: LiveState["connection"]; onReconnect: () => void }) {
  if (state === "open") {
    return (
      <Badge variant="success" dot pulse className="h-9 px-3">
        <PlugZap className="size-3.5" /> Live feed connected
      </Badge>
    );
  }
  return (
    <button type="button" onClick={onReconnect} className="focus-visible:outline-none">
      <Badge variant={state === "connecting" ? "info" : "danger"} dot pulse={state === "connecting"} className="h-9 px-3">
        <Plug className="size-3.5" /> {state === "connecting" ? "Connecting to feed" : "Feed disconnected · retry"}
      </Badge>
    </button>
  );
}

/**
 * Says, in the one place someone is actually watching, whether anything from
 * this call is being kept.
 *
 * A delegate that is present but retaining nothing looks identical to one that
 * is working, which is the worst possible ambiguity for a compliance feature:
 * the principal needs to know the meeting they are watching will produce no
 * briefing, *while* it is happening, not afterwards.
 */
function ConsentStrip({ status }: { status: DelegateStatus | null | undefined }) {
  const toast = useToast();
  const [busy, setBusy] = React.useState(false);
  if (!status?.active || status.consent_mode === "notify_only") return null;
  if (status.retaining && status.consent_state !== "granted") return null;

  const pending = status.consent_state === "pending";
  const refused = ["denied", "timed_out", "withdrawn"].includes(status.consent_state);
  if (!pending && !refused && status.consent_state !== "granted") return null;

  async function resolve(granted: boolean) {
    if (!status?.meeting_id) return;
    setBusy(true);
    try {
      await api.resolveConsent(status.meeting_id, granted);
      toast.push({
        title: granted ? "Consent recorded" : "Refusal recorded",
        description: granted
          ? "The delegate will take notes from here."
          : "The delegate will say so and leave without keeping anything.",
        variant: "success",
      });
    } catch (err) {
      toast.push({ title: "Could not record it", description: errorMessage(err), variant: "error" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      role="status"
      className={cn(
        "mt-4 flex flex-wrap items-start gap-3 rounded-lg border px-4 py-3 text-sm",
        pending ? "border-warning/50 bg-warning/10" : refused ? "border-danger/50 bg-danger/10" : "border-success/40 bg-success/5",
      )}
    >
      <ShieldAlert className={cn("mt-0.5 size-4 shrink-0", pending ? "text-warning" : refused ? "text-danger" : "text-success")} />
      <div className="min-w-0 flex-1">
        <p className="font-medium">
          {pending
            ? "Waiting for the room to agree before anything is kept"
            : refused
              ? `Consent ${status.consent_state.replace("_", " ")} - nothing from this call is being kept`
              : "Consent granted - the transcript is being kept"}
        </p>
        <p className="mt-0.5 text-xs text-muted-foreground">
          {pending
            ? "Every utterance is being read for a yes or no and then discarded. No transcript, memory, summary or action items until someone agrees."
            : refused
              ? "The delegate has announced that it will not transcribe, and is leaving."
              : "The consent record is on your Settings page."}
        </p>
      </div>
      {pending ? (
        <div className="flex shrink-0 gap-2">
          <Button size="sm" onClick={() => resolve(true)} loading={busy}>
            I have their consent
          </Button>
          <Button size="sm" variant="outline" onClick={() => resolve(false)} loading={busy}>
            Refuse and leave
          </Button>
        </div>
      ) : null}
    </div>
  );
}

const ACTIVITY_ICON: Record<DelegateActivity, React.ComponentType<{ className?: string }>> = {
  IDLE: Clock,
  CONNECTING: Plug,
  LISTENING: Ear,
  THINKING: Brain,
  AWAITING_APPROVAL: Send,
  AWAITING_CONSENT: ShieldAlert,
  SPEAKING: Volume2,
  LEAVING: ArrowDownToLine,
};

const ACTIVITY_TONE: Record<DelegateActivity, string> = {
  IDLE: "text-muted-foreground bg-muted",
  CONNECTING: "text-info bg-info/10",
  LISTENING: "text-success bg-success/10",
  THINKING: "text-warning-foreground bg-warning/15",
  AWAITING_APPROVAL: "text-danger bg-danger/10",
  AWAITING_CONSENT: "text-warning-foreground bg-warning/15",
  SPEAKING: "text-violet bg-violet/10",
  LEAVING: "text-muted-foreground bg-muted",
};

function StatusStrip({ status, connection }: { status: DelegateStatus | null; connection: LiveState["connection"] }) {
  const now = useNow();
  const activity: DelegateActivity = status?.activity ?? "IDLE";
  const Icon = ACTIVITY_ICON[activity];
  const since = status?.joined_at ?? status?.started_at ?? null;
  const elapsed = status?.active && since ? formatDuration((now - new Date(since).getTime()) / 1000) : null;
  const counters = status?.counters;

  return (
    <Card className="border-white/10 bg-white/[0.035] shadow-none">
      <CardContent className="flex flex-col gap-4 p-4 lg:flex-row lg:items-center">
        <div className="flex items-center gap-3">
          <span className={cn("grid size-11 shrink-0 place-items-center rounded-lg", ACTIVITY_TONE[activity])}>
            {activity === "SPEAKING" ? <SpeakingBars /> : <Icon className={cn("size-5", activity !== "IDLE" && activity !== "LEAVING" && "pulse-dot")} />}
          </span>
          <div className="leading-tight">
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Delegate status</div>
            <div className="text-lg font-semibold tracking-tight">
              [{ACTIVITY_LABEL[activity]}]
              {activity === "AWAITING_APPROVAL" && status?.pending_escalations ? <span className="ml-2 text-xs font-normal text-muted-foreground">{status.pending_escalations} pending on Telegram</span> : null}
            </div>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2 lg:ml-2">
          {status ? <PhaseBadge phase={status.phase} active={status.active} /> : null}
          {status?.meeting_code ? (
            <span className="inline-flex items-center gap-1.5 font-mono text-sm">
              {status.meeting_code}
              {status.join_url ? (
                <a href={status.join_url} target="_blank" rel="noreferrer" className="text-muted-foreground hover:text-foreground" aria-label="Open in Google Meet">
                  <ExternalLink className="size-3.5" />
                </a>
              ) : null}
            </span>
          ) : null}
          {status?.detail && status.active ? <span className="max-w-xs truncate text-xs text-muted-foreground" title={status.detail}>{status.detail}</span> : null}
        </div>
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2 lg:ml-auto">
          {elapsed ? <Counter label={status?.joined_at ? "in call" : "elapsed"} value={elapsed} /> : null}
          <Counter label="heard" value={counters?.utterances ?? 0} />
          <Counter label="answered" value={counters?.auto_replies ?? 0} />
          <Counter label="escalated" value={counters?.escalations ?? 0} />
          <Counter label="interrupted" value={counters?.interruptions ?? 0} />
          {counters?.brain_avg_ms ? <Counter label="brain" value={`${Math.round(counters.brain_avg_ms)} ms`} /> : null}
          {connection !== "open" ? <Badge variant="outline">feed {connection}</Badge> : null}
        </div>
      </CardContent>
    </Card>
  );
}

function Counter({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="leading-tight">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className="tabular text-sm font-semibold">{value}</div>
    </div>
  );
}

function SpeakingBars() {
  return (
    <span className="flex h-5 items-center gap-0.5" aria-hidden="true">
      {[0, 1, 2, 3].map((i) => (
        <span key={i} className="speak-bar block h-full w-1 rounded-full bg-current" style={{ animationDelay: `${i * 120}ms` }} />
      ))}
    </span>
  );
}

function NowPanel({ status, state }: { status: DelegateStatus | null; state: LiveState }) {
  const speaking = status?.activity === "SPEAKING";
  const speaker = speaking ? "Out of Office" : state.interim ? state.interimSpeaker ?? status?.speaker_label ?? "Meeting" : status?.current_speaker ?? (state.turns.length ? state.turns[state.turns.length - 1].speaker : null);
  const text = speaking ? status?.speaking_text : state.interim || status?.last_utterance || (state.turns.length ? state.turns[state.turns.length - 1].text : null);
  return (
    <Card>
      <CardContent className="grid gap-4 p-4 sm:grid-cols-[1fr_auto]">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-[10px] uppercase tracking-wider text-muted-foreground">
            {speaking ? <Volume2 className="size-3.5 text-violet" /> : <Ear className="size-3.5" />} Active speaker
          </div>
          <div className="mt-1 flex items-baseline gap-2">
            <span className={cn("text-base font-semibold", speaking && "text-violet")}>{speaker ?? "—"}</span>
            {state.interim && !speaking ? <span className="text-[11px] text-muted-foreground">live caption</span> : null}
          </div>
          <p className={cn("mt-1 min-h-[1.25rem] text-sm leading-relaxed", state.interim && !speaking ? "italic text-muted-foreground" : "text-foreground")}>
            {text || <span className="text-muted-foreground">Waiting for speech…</span>}
          </p>
        </div>
        <div className="sm:min-w-44 sm:border-l sm:border-border sm:pl-4">
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Meeting topic</div>
          <div className="mt-1 text-base font-semibold capitalize">{humanTopic(status?.active_topic) === "—" ? <span className="font-normal text-muted-foreground">not yet inferred</span> : humanTopic(status?.active_topic)}</div>
          {state.participants.length ? <div className="mt-2 text-xs text-muted-foreground">{state.participants.join(" · ")}</div> : null}
        </div>
      </CardContent>
    </Card>
  );
}

function Transcript({ state }: { state: LiveState }) {
  const [autoScroll, setAutoScroll] = React.useState(true);
  const boxRef = React.useRef<HTMLDivElement>(null);
  const count = state.turns.length;

  React.useEffect(() => {
    if (!autoScroll || !boxRef.current) return;
    boxRef.current.scrollTop = boxRef.current.scrollHeight;
  }, [count, state.interim, autoScroll]);

  const onScroll = () => {
    const box = boxRef.current;
    if (!box) return;
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    setAutoScroll(atBottom);
  };

  return (
    <Card className="flex min-h-[420px] flex-col">
      <CardHeader className="flex-row items-center justify-between pb-2">
        <div>
          <CardTitle>Live transcript</CardTitle>
          <CardDescription>Everything said in the meeting, with what your delegate decided to do about it.</CardDescription>
        </div>
        {!autoScroll ? (
          <Button variant="outline" size="sm" onClick={() => setAutoScroll(true)}>
            <ArrowDownToLine className="size-3.5" /> Follow
          </Button>
        ) : null}
      </CardHeader>
      <CardContent className="flex-1 pb-4">
        <div ref={boxRef} onScroll={onScroll} className="scroll-thin max-h-[60vh] min-h-[340px] overflow-y-auto pr-1">
          {state.turns.length === 0 && !state.interim ? (
            <EmptyState
              compact
              icon={<Ear />}
              title="Nothing heard yet"
              description={
                state.audio.inputDevice
                  ? `Listening on ${state.audio.inputDevice}. Words appear here as people speak.`
                  : "Words appear here as people speak."
              }
            />
          ) : (
            <ol className="flex flex-col gap-3">
              {state.turns.map((line) => (
                <TranscriptRow key={line.id} line={line} />
              ))}
              {state.interim ? (
                <li className="flex gap-3 fade-in">
                  <SpeakerTag name={state.interimSpeaker ?? "Meeting"} />
                  <p className="text-sm italic text-muted-foreground">{state.interim}</p>
                </li>
              ) : null}
            </ol>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function SpeakerTag({ name, delegate = false }: { name: string; delegate?: boolean }) {
  return (
    <span className={cn("mt-0.5 inline-flex h-5 shrink-0 items-center gap-1 rounded px-1.5 text-[11px] font-semibold", delegate ? "bg-violet/15 text-violet" : "bg-muted text-muted-foreground")}>
      {delegate ? <Bot className="size-3" /> : null}
      {name}
    </span>
  );
}

function TranscriptRow({ line }: { line: TranscriptLine }) {
  const [open, setOpen] = React.useState(false);
  return (
    <li className={cn("flex gap-3 fade-in", line.isDelegate && "rounded-md border border-violet/25 bg-violet/5 p-2.5")}>
      <SpeakerTag name={line.speaker} delegate={line.isDelegate} />
      <div className="min-w-0 flex-1">
        <p className={cn("text-sm leading-relaxed", line.interrupted && "line-through decoration-muted-foreground/60")}>{line.text}</p>
        <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
          <span className="tabular">{formatTime(line.at)}</span>
          {line.isDelegate ? (
            line.interrupted ? <Badge variant="outline">interrupted</Badge> : null
          ) : line.pending ? (
            <Badge variant="warning" dot pulse>thinking</Badge>
          ) : (
            <>
              <DecisionBadge state={line.decision} />
              {line.tier ? <TierBadge tier={line.tier} /> : null}
              {line.topic ? <span>{humanTopic(line.topic)}</span> : null}
              {line.brainMs !== null ? <span className="tabular">{line.brainMs} ms</span> : null}
              {line.interruptedBot ? <Badge variant="outline">barge-in</Badge> : null}
              {line.escalation ? <Badge variant={line.escalation.human_decided ? "info" : "outline"}>{line.escalation.outcome.toLowerCase().replace(/_/g, " ")}{line.escalation.responded_by ? ` · ${line.escalation.responded_by}` : ""}</Badge> : null}
              {line.reason ? (
                <button type="button" onClick={() => setOpen((v) => !v)} className="underline-offset-2 hover:underline">
                  {open ? "hide why" : "why?"}
                </button>
              ) : null}
            </>
          )}
        </div>
        {open && line.reason ? (
          <div className="mt-1.5 rounded border border-border bg-muted/50 p-2 text-xs text-muted-foreground">
            <p>{line.reason}</p>
            {line.grounding ? (
              <p className="mt-1">
                Based on: {line.grounding.citations.join(", ") || line.grounding.subject}
              </p>
            ) : null}
            {line.proposedResponse && line.decision === "ESCALATE" ? <p className="mt-1">Proposed to the human: “{line.proposedResponse}”</p> : null}
          </div>
        ) : null}
      </div>
    </li>
  );
}

function SideCard({ title, icon, count, children, description }: { title: string; icon: React.ReactNode; count: number; children: React.ReactNode; description?: string }) {
  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between pb-2">
        <div>
          <CardTitle className="flex items-center gap-2">
            {icon} {title}
          </CardTitle>
          {description ? <CardDescription>{description}</CardDescription> : null}
        </div>
        <Badge variant="outline" className="tabular">{count}</Badge>
      </CardHeader>
      <CardContent className="pb-4">{children}</CardContent>
    </Card>
  );
}

function Escalations({ entries, pending }: { entries: EscalationEntry[]; pending: number }) {
  const recent = entries.slice(-8).reverse();
  return (
    <SideCard title="Telegram escalations" icon={<MessageSquareWarning className="size-4 text-muted-foreground" />} count={entries.length} description={pending ? `${pending} awaiting a tap` : undefined}>
      {recent.length === 0 ? (
        <p className="text-xs text-muted-foreground">Nothing escalated yet.</p>
      ) : (
        <ul className="flex flex-col gap-2.5">
          {recent.map((e) => (
            <li key={e.id} className="rounded-md border border-border p-2.5 text-xs">
              <div className="flex items-center gap-1.5">
                {e.outcome === "PENDING" ? <Badge variant="danger" dot pulse>awaiting approval</Badge> : <Badge variant={e.outcome === "APPROVED" ? "success" : e.outcome === "TIMEOUT" || e.outcome === "UNDELIVERED" ? "outline" : "info"}>{e.outcome.toLowerCase().replace(/_/g, " ")}</Badge>}
                <TierBadge tier={e.tier} />
                <span className="ml-auto tabular text-muted-foreground">{formatTime(e.at)}</span>
              </div>
              <p className="mt-1.5 leading-relaxed">“{e.transcript}”</p>
              {e.reason ? <p className="mt-1 text-muted-foreground">{e.reason}</p> : null}
              {e.outcome !== "PENDING" ? (
                <p className="mt-1 text-muted-foreground">
                  {e.respondedBy ? `${e.respondedBy} · ` : ""}
                  {e.responseSeconds !== null ? `${e.responseSeconds.toFixed(1)} s` : ""}
                  {e.response ? ` · said: “${e.response}”` : ""}
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </SideCard>
  );
}

function ActionItems({ items }: { items: ActionItem[] }) {
  return (
    <SideCard title="Action items" icon={<ListChecks className="size-4 text-muted-foreground" />} count={items.length}>
      {items.length === 0 ? (
        <p className="text-xs text-muted-foreground">Detected live from phrases like “Sarah will send the deck by Friday”.</p>
      ) : (
        <ul className="flex flex-col divide-y divide-border">
          {[...items].reverse().map((a, i) => (
            <li key={`${a.turn_index}-${i}`} className="py-2 text-xs first:pt-0 last:pb-0">
              <p className="font-medium leading-relaxed">{a.description}</p>
              <p className="mt-0.5 text-muted-foreground">
                {a.owner}
                {a.owner_inferred ? " (inferred)" : ""}
                {a.due_date ? ` · due ${a.due_date}` : a.due_text ? ` · ${a.due_text}` : ""}
              </p>
            </li>
          ))}
        </ul>
      )}
    </SideCard>
  );
}

function Decisions({ items }: { items: AgreedDecision[] }) {
  return (
    <SideCard title="Decisions" icon={<Gavel className="size-4 text-muted-foreground" />} count={items.length}>
      {items.length === 0 ? (
        <p className="text-xs text-muted-foreground">Agreements voiced in the room and verdicts you tap on Telegram.</p>
      ) : (
        <ul className="flex flex-col divide-y divide-border">
          {[...items].reverse().map((d, i) => (
            <li key={`${d.turn_index}-${i}`} className="flex gap-2 py-2 text-xs first:pt-0 last:pb-0">
              <CheckCircle2 className={cn("mt-0.5 size-3.5 shrink-0", d.origin === "human_rejected" ? "text-danger" : d.origin === "human_approved" ? "text-info" : "text-success")} />
              <div>
                <p className="leading-relaxed">{d.text}</p>
                <p className="mt-0.5 text-muted-foreground">{d.proposed_by}{d.origin !== "utterance" ? " · via Telegram" : ""}</p>
              </div>
            </li>
          ))}
        </ul>
      )}
    </SideCard>
  );
}

function OpenQuestions({ items }: { items: OpenQuestion[] }) {
  return (
    <SideCard title="Open questions" icon={<HelpCircle className="size-4 text-muted-foreground" />} count={items.length}>
      {items.length === 0 ? (
        <p className="text-xs text-muted-foreground">Requests the delegate could not settle itself.</p>
      ) : (
        <ul className="flex flex-col divide-y divide-border">
          {[...items].reverse().map((q, i) => (
            <li key={`${q.turn_index}-${i}`} className="py-2 text-xs first:pt-0 last:pb-0">
              <p className="leading-relaxed">“{q.text}”</p>
              <p className="mt-0.5 text-muted-foreground">{q.asked_by} · {q.outcome}</p>
            </li>
          ))}
        </ul>
      )}
    </SideCard>
  );
}

function Timeline({ entries, className }: { entries: TimelineEntry[]; className?: string }) {
  const [open, setOpen] = React.useState(false);
  const shown = open ? entries : entries.slice(-6);
  return (
    <Card className={className}>
      <CardHeader className="flex-row items-center justify-between pb-2">
        <CardTitle className="flex items-center gap-2">
          <Clock className="size-4 text-muted-foreground" /> Session timeline
        </CardTitle>
        {entries.length > 6 ? (
          <button type="button" onClick={() => setOpen((v) => !v)} className="text-xs text-muted-foreground hover:text-foreground">
            {open ? "show recent" : `all ${entries.length}`}
          </button>
        ) : null}
      </CardHeader>
      <CardContent className="pb-4">
        {shown.length === 0 ? (
          <p className="text-xs text-muted-foreground">Browser and call lifecycle events appear here.</p>
        ) : (
          <ol className="flex flex-col gap-1.5 text-xs">
            {[...shown].reverse().map((t) => (
              <li key={t.id} className="flex gap-2">
                <span className="tabular w-20 shrink-0 whitespace-nowrap text-muted-foreground">{formatTime(t.at)}</span>
                <span className={cn("shrink-0", t.kind === "phase" ? "font-medium" : "text-muted-foreground")}>{t.label}</span>
                {t.detail ? <span className="min-w-0 truncate text-muted-foreground" title={t.detail}>· {t.detail}</span> : null}
              </li>
            ))}
          </ol>
        )}
      </CardContent>
    </Card>
  );
}

function LastBriefing({ state }: { state: LiveState }) {
  const report = state.lastReport;
  if (!report) return null;
  return (
    <Card className="mt-4 border-info/30">
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2">
          <AlertTriangle className="size-4 text-info" /> Executive briefing sent
        </CardTitle>
        <CardDescription>
          {report.receipt.delivered ? "Sent to your phone" : "Could not be sent to your phone"}
        </CardDescription>
      </CardHeader>
      <CardContent className="pb-4 text-sm leading-relaxed">
        <p>{report.briefing.executive_summary}</p>
        <Link href="/history" className="mt-2 inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
          Open in Briefs <ExternalLink className="size-3" />
        </Link>
      </CardContent>
    </Card>
  );
}
