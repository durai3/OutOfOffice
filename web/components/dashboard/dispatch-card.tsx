"use client";

import * as React from "react";
import Link from "next/link";
import { ChevronDown, ExternalLink, OctagonX, Radio, Rocket } from "lucide-react";
import { api, errorMessage } from "@/lib/api";
import type { ConsentMode, DelegateStatus } from "@/lib/types";
import { cn, detectMeetingPlatform, formatDuration, meetingCode, meetingIdPrefix, PLATFORM_LABELS } from "@/lib/utils";
import { useToast } from "@/components/toast";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Hint, Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { ActivityBadge, PhaseBadge } from "@/components/status-badge";

function useNow(intervalMs = 1000): number {
  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}

export function DispatchCard({ status, onChanged }: { status: DelegateStatus | undefined; onChanged: () => void }) {
  const toast = useToast();
  const [url, setUrl] = React.useState("");
  const [meetingId, setMeetingId] = React.useState("");
  const [speaker, setSpeaker] = React.useState("Meeting");
  const [maxMinutes, setMaxMinutes] = React.useState("");
  const [halfDuplex, setHalfDuplex] = React.useState(false);
  const [briefing, setBriefing] = React.useState(true);
  const [consentMode, setConsentMode] = React.useState<ConsentMode | "">("");
  const [advanced, setAdvanced] = React.useState(false);
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [confirmRecall, setConfirmRecall] = React.useState(false);
  const [recalling, setRecalling] = React.useState(false);

  const platform = detectMeetingPlatform(url);
  const code = meetingCode(url);
  const urlInvalid = url.trim().length > 0 && !platform;
  const active = Boolean(status?.active);

  async function launch(event: React.FormEvent) {
    event.preventDefault();
    if (!platform) {
      setError("Enter a Google Meet, Zoom, or Microsoft Teams link");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const minutes = maxMinutes.trim() ? Number(maxMinutes) : null;
      const result = await api.join({
        url: url.trim(),
        meeting_id: meetingId.trim() || null,
        speaker_label: speaker.trim() || "Meeting",
        half_duplex: halfDuplex,
        briefing,
        max_minutes: minutes && Number.isFinite(minutes) && minutes > 0 ? minutes : null,
        consent_mode: consentMode || null,
      });
      toast.push({
        title: `Delegate dispatched to ${result.meeting_code}`,
        description:
          result.runtime === "container"
            ? "Running in its own container. Follow along on the Live Monitor."
            : "Joining the call now. Follow along on the Live Monitor.",
        variant: "success",
      });
      setUrl("");
      onChanged();
    } catch (err) {
      const message = errorMessage(err);
      setError(message);
      toast.push({ title: "Could not dispatch the delegate", description: message, variant: "error" });
    } finally {
      setSubmitting(false);
    }
  }

  async function recall() {
    setRecalling(true);
    try {
      await api.abort("Recalled from the dashboard");
      toast.push({ title: "Recall issued", description: "The delegate is leaving the call and will send its briefing.", variant: "success" });
      setConfirmRecall(false);
      onChanged();
    } catch (err) {
      toast.push({ title: "Recall failed", description: errorMessage(err), variant: "error" });
    } finally {
      setRecalling(false);
    }
  }

  return (
    <>
      <Card className="relative overflow-hidden rounded-2xl border-white/10 bg-transparent p-0 shadow-[0_24px_50px_-24px_rgba(0,0,0,0.6)]">
        <div className="dark relative isolate overflow-hidden rounded-[inherit] bg-[radial-gradient(90%_70%_at_100%_0%,#26262b_0%,transparent_60%),linear-gradient(180deg,#151517_0%,#0e0e10_100%)]">
          <div
            aria-hidden="true"
            className="pointer-events-none absolute inset-0 bg-[linear-gradient(rgba(255,255,255,0.035)_1px,transparent_1px),linear-gradient(90deg,rgba(255,255,255,0.035)_1px,transparent_1px)] [background-size:28px_28px] [mask-image:linear-gradient(120deg,transparent_30%,#000_100%)]"
          />
          <CardHeader className="relative flex-row items-start justify-between gap-4 border-b border-white/10 pb-4">
            <div>
              <div className="flex items-center gap-2 font-mono text-[10.5px] font-medium tracking-[0.14em] text-[#d8d8de]">DISPATCH</div>
              <CardTitle className="mt-2 flex items-center gap-2 text-base text-white">
                <Rocket className="size-4 text-white/60" /> Dispatch Delegate
              </CardTitle>
              <CardDescription className="mt-1 max-w-md text-white/55">
                Send DELEG8 into a Google Meet, Zoom, or Microsoft Teams call. It joins with the camera off, listens,
                answers within policy, and escalates the rest to Telegram.
              </CardDescription>
            </div>
            <div className="flex shrink-0 flex-col items-end gap-2">
              <span className="inline-flex items-center gap-1.5 font-mono text-[11px] tracking-[0.08em] text-white/45">
                <span className={cn("size-1.5 rounded-full", active ? "bg-success" : "bg-white/40")} />
                {active ? "ACTIVE" : "ARMED"}
              </span>
              {status ? <ActivityBadge activity={status.activity} /> : null}
            </div>
          </CardHeader>
          <CardContent className="relative pt-5">
            {active && status ? (
              <ActiveSession status={status} onRecall={() => setConfirmRecall(true)} />
            ) : (
              <form onSubmit={launch} className="flex flex-col gap-4" noValidate>
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="meet-url" className="text-white/60">Meeting URL</Label>
                  <div className="flex flex-col gap-2 sm:flex-row">
                    <Input
                      id="meet-url"
                      value={url}
                      onChange={(e) => setUrl(e.target.value)}
                      placeholder="https://meet.google.com/abc-defg-hij, a Zoom, or a Teams link"
                      autoComplete="off"
                      spellCheck={false}
                      aria-invalid={urlInvalid || undefined}
                      className="border-white/10 bg-white/5 font-mono text-[13px] text-white placeholder:text-white/35"
                    />
                    <Button type="submit" loading={submitting} disabled={!platform} className="sm:w-44">
                      {submitting ? "Dispatching" : "Launch Delegate"}
                    </Button>
                  </div>
                  {urlInvalid ? (
                    <Hint className="text-danger">That does not look like a Google Meet, Zoom, or Microsoft Teams link.</Hint>
                  ) : platform ? (
                    <Hint className="text-white/45">
                      {PLATFORM_LABELS[platform]}
                      {code ? (
                        <>
                          , code <span className="font-mono">{code}</span>. Memory id defaults to{" "}
                          <span className="font-mono">{meetingIdPrefix(platform)}-{code}</span>
                        </>
                      ) : null}
                      .
                    </Hint>
                  ) : (
                    <Hint className="text-white/45">Paste the invitation link. The host must admit the bot from the lobby unless it signs in with an invited account.</Hint>
                  )}
                </div>

                <button type="button" onClick={() => setAdvanced((v) => !v)} className="flex w-fit items-center gap-1 text-xs text-white/50 hover:text-white" aria-expanded={advanced}>
                  <ChevronDown className={cn("size-3.5 transition-transform", advanced && "rotate-180")} /> Session options
                </button>

                {advanced ? (
                  <div className="grid gap-4 rounded-md border border-white/10 bg-white/[0.04] p-4 sm:grid-cols-2">
                    <div className="flex flex-col gap-1.5">
                      <Label htmlFor="meeting-id" className="text-white/60">Meeting id</Label>
                      <Input
                        id="meeting-id"
                        value={meetingId}
                        onChange={(e) => setMeetingId(e.target.value)}
                        placeholder={platform && code ? `${meetingIdPrefix(platform)}-${code}` : "meet-<code>"}
                        className="border-white/10 bg-white/5 font-mono text-[13px] text-white placeholder:text-white/35"
                      />
                      <Hint className="text-white/40">Used for memory and the briefing filename.</Hint>
                    </div>
                    <div className="flex flex-col gap-1.5">
                      <Label htmlFor="speaker" className="text-white/60">Label for the room</Label>
                      <Input id="speaker" value={speaker} onChange={(e) => setSpeaker(e.target.value)} placeholder="Meeting" className="border-white/10 bg-white/5 text-white placeholder:text-white/35" />
                      <Hint className="text-white/40">No diarization yet: everyone heard is tagged with this name.</Hint>
                    </div>
                    <div className="flex flex-col gap-1.5">
                      <Label htmlFor="max-minutes" className="text-white/60">Leave automatically after (minutes)</Label>
                      <Input id="max-minutes" inputMode="decimal" value={maxMinutes} onChange={(e) => setMaxMinutes(e.target.value)} placeholder="no limit" className="border-white/10 bg-white/5 text-white placeholder:text-white/35" />
                    </div>
                    <div className="flex flex-col gap-1.5">
                      <Label htmlFor="consent-mode" className="text-white/60">Recording consent</Label>
                      <Select
                        id="consent-mode"
                        value={consentMode}
                        onChange={(e) => setConsentMode(e.target.value as ConsentMode | "")}
                        className="border-white/10 bg-white/5 text-white"
                      >
                        <option value="">Use my default (Settings)</option>
                        <option value="notify_only">Announce only</option>
                        <option value="all_party">Wait for everyone to agree</option>
                        <option value="single_party">Single-party consent</option>
                      </Select>
                      <Hint className="text-white/40">
                        In all-party mode nothing is kept - no transcript, memory, summary or action items - until
                        someone in the room says yes.
                      </Hint>
                    </div>
                    <div className="flex flex-col gap-3 pt-1">
                      <div className="flex items-center justify-between gap-3">
                        <Label htmlFor="half-duplex" className="font-normal text-white/70">Half-duplex (mute mic while speaking)</Label>
                        <Switch id="half-duplex" checked={halfDuplex} onCheckedChange={setHalfDuplex} />
                      </div>
                      <div className="flex items-center justify-between gap-3">
                        <Label htmlFor="briefing" className="font-normal text-white/70">Send executive briefing on exit</Label>
                        <Switch id="briefing" checked={briefing} onCheckedChange={setBriefing} />
                      </div>
                    </div>
                  </div>
                ) : null}

                {error ? <p className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">{error}</p> : null}

                {status?.finished_at && (status.error || status.stop_reason) ? (
                  <div className="rounded-md border border-white/10 bg-white/[0.04] px-3 py-2 text-xs text-white/55">
                    <span className="font-medium text-white">Last session</span> · {status.meeting_code} · {status.error ? <span className="text-danger">{status.error}</span> : status.stop_reason}
                  </div>
                ) : null}
              </form>
            )}
          </CardContent>
        </div>
      </Card>

      <Dialog
        open={confirmRecall}
        onClose={() => setConfirmRecall(false)}
        title="Recall the delegate?"
        description="It stops speaking immediately, leaves the call, closes the browser, and sends the executive briefing."
        footer={
          <>
            <Button variant="outline" onClick={() => setConfirmRecall(false)}>Cancel</Button>
            <Button variant="destructive" onClick={recall} loading={recalling}>
              <OctagonX className="size-4" /> Recall now
            </Button>
          </>
        }
      >
        <p className="text-sm text-muted-foreground">Meeting <span className="font-mono text-foreground">{status?.meeting_code}</span> will end for the delegate. Participants stay in the call.</p>
      </Dialog>
    </>
  );
}

function ActiveSession({ status, onRecall }: { status: DelegateStatus; onRecall: () => void }) {
  const now = useNow();
  const since = status.joined_at ?? status.started_at;
  const elapsed = since ? formatDuration((now - new Date(since).getTime()) / 1000) : "—";
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <PhaseBadge phase={status.phase} active />
        <span className="font-mono text-sm text-white">{status.meeting_code}</span>
        {status.join_url ? (
          <a href={status.join_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-xs text-white/50 hover:text-white">
            open in {status.platform ? PLATFORM_LABELS[status.platform] : "the call"} <ExternalLink className="size-3" />
          </a>
        ) : null}
        <span className="ml-auto tabular text-xs text-white/45">{status.joined_at ? "in call" : "elapsed"} {elapsed}</span>
      </div>
      <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
        <Fact label="Heard" value={status.counters.utterances} />
        <Fact label="Answered" value={status.counters.auto_replies} />
        <Fact label="Escalated" value={status.counters.escalations} />
        <Fact label="Topic" value={status.active_topic ?? "—"} mono={false} />
      </dl>
      {status.detail ? <p className="truncate text-xs text-white/45" title={status.detail}>{status.detail}</p> : null}
      <div className="flex flex-wrap gap-2">
        <Link href="/live" className={buttonVariants()}>
          <Radio className="size-4" /> Open Live Monitor
        </Link>
        <Button variant="destructive" onClick={onRecall}>
          <OctagonX className="size-4" /> Emergency Recall
        </Button>
      </div>
    </div>
  );
}

function Fact({ label, value, mono = true }: { label: string; value: React.ReactNode; mono?: boolean }) {
  return (
    <div className="rounded-md border border-white/10 bg-white/[0.04] px-3 py-2">
      <dt className="text-[10px] uppercase tracking-wider text-white/40">{label}</dt>
      <dd className={cn("truncate text-sm font-medium text-white", mono && "tabular")}>{value}</dd>
    </div>
  );
}
