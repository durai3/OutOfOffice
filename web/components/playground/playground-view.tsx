"use client";

import * as React from "react";
import Link from "next/link";
import {
  ArrowRight,
  Ban,
  Bot,
  Loader2,
  Check,
  CircleDot,
  FileText,
  ListChecks,
  Play,
  Quote,
  RotateCcw,
  Send,
  ShieldCheck,
  Smartphone,
  Sparkles,
  Square,
  Volume2,
  VolumeX,
  X,
} from "lucide-react";
import { api, errorMessage } from "@/lib/api";
import { useApi } from "@/hooks/use-api";
import { PageHeader } from "@/components/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { DecisionBadge, TierBadge } from "@/components/status-badge";
import { useToast } from "@/components/toast";
import { BriefingCard } from "@/components/playground/briefing";
import { Switch } from "@/components/ui/switch";
import { useTechDetails } from "@/lib/tech-details";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/utils";
import { useVoice } from "./use-voice";
import type {
  PlaygroundApproval,
  PlaygroundEvent,
  PlaygroundSession,
  PlaygroundTurnResult,
  Scenario,
} from "@/lib/types";

/**
 * Run a whole meeting from the browser.
 *
 * Nothing on this page is a mock. A line typed here goes through the same
 * pipeline a live call uses - floor check, policy sensitivity, the Gemini
 * response planner, the output guard, meeting memory, the commitment ledger -
 * over this account's own documents. Only the two edges differ: speech arrives
 * as text, and the "phone" that approves risky replies is the panel on the
 * right instead of Telegram. It is the fastest way to see the product work and
 * the safest way to rehearse a policy before trusting it in a real room.
 */

type Busy = "start" | "say" | "conclude" | "sample" | `resolve:${string}` | null;

const DEFAULT_SPEAKER = "Sofia";
const DEFAULT_PARTICIPANTS = ["Sofia", "Jonas"];

/** What each step is called for everyone else. The technical names above are for evaluators. */
const PLAIN_STAGE_LABEL: Record<string, string> = {
  FLOOR_CHECK: "Checked it was meant for your delegate",
  SENSITIVITY: "Checked your rules",
  PLANNER: "Worked out a reply",
  GUARD: "Fact-checked the reply",
  LEAVE_REQUEST: "Leave request",
  AUTO_REPLY: "Answered",
  ESCALATE: "Asked you first",
  SILENT: "Stayed quiet",
};

const STAGE_LABEL: Record<string, string> = {
  FLOOR_CHECK: "Was it for me?",
  SENSITIVITY: "Policy check",
  PLANNER: "Gemini planner",
  GUARD: "Output guard",
  LEAVE_REQUEST: "Leave request",
  AUTO_REPLY: "Decision: speak",
  ESCALATE: "Decision: ask the human",
  SILENT: "Decision: stay silent",
};

const GUARD_EXPLAIN: Record<string, string> = {
  passed: "Every factual claim in the reply was found in the source documents, and no restricted subject appeared. It was spoken as planned.",
  rewritten: "A claim could not be verified against the documents, so the reply was replaced with an honest “I'll check and come back to you” instead of a guess.",
  blocked: "A restricted subject (money, contracts, personal data) appeared in the output, so nothing was said and a human was asked instead.",
};

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function providerOf(model: string): string {
  const head = model.split(/[\s(]/)[0] ?? "";
  if (head === "gemini") return "Gemini";
  if (head === "claude") return "Claude";
  return head || "No model";
}

export function PlaygroundView() {
  const toast = useToast();
  const scenarios = useApi(api.playgroundScenarios);
  const knowledge = useApi(api.knowledge);
  const { account } = useAuth();
  const [tech, setTech] = useTechDetails();
  const judge = account?.settings?.demo === true;
  const autoLoaded = React.useRef(false);

  const [session, setSession] = React.useState<PlaygroundSession | null>(null);
  const [loadingSession, setLoadingSession] = React.useState(true);
  const [busy, setBusy] = React.useState<Busy>(null);
  const [results, setResults] = React.useState<Record<number, PlaygroundTurnResult>>({});
  const [selected, setSelected] = React.useState<number | null>(null);
  const [speaker, setSpeaker] = React.useState(DEFAULT_SPEAKER);
  const [text, setText] = React.useState("");
  const [scenario, setScenario] = React.useState<Scenario | null>(null);
  const [cursor, setCursor] = React.useState(0);
  const [running, setRunning] = React.useState(false);

  const stopRef = React.useRef(false);
  const busyRef = React.useRef<Busy>(null);
  busyRef.current = busy;
  const pendingRef = React.useRef(0);
  const feedRef = React.useRef<HTMLDivElement>(null);

  // Spoken meeting: people in the room share one voice family, the delegate has its own.
  const voice = useVoice();
  const spokenUpTo = React.useRef<number | null>(null);
  const liveSpoken = React.useRef<string[]>([]);

  /** Speak every event not yet heard, then show the session. Lines already voiced live are skipped. */
  function apply(next: PlaygroundSession) {
    if (spokenUpTo.current === null) {
      spokenUpTo.current = Math.max(-1, ...next.events.map((e) => e.seq));
    } else {
      const from = spokenUpTo.current;
      for (const event of next.events) {
        if (event.seq <= from) continue;
        spokenUpTo.current = Math.max(spokenUpTo.current, event.seq);
        if (event.kind === "utterance" && event.text) {
          const live = liveSpoken.current.indexOf(event.text);
          if (live >= 0) liveSpoken.current.splice(live, 1);
          else void voice.speak("human", event.speaker ?? "Guest", event.text);
        } else if (event.kind === "delegate" && event.text) {
          void voice.speak("delegate", "Delegate", event.text);
        }
      }
    }
    setSession(next);
  }

  const documentCount = knowledge.data?.documents.length ?? null;
  const pending = session?.approvals.filter((a) => a.pending) ?? [];
  pendingRef.current = pending.length;
  const live = session !== null && !session.concluded;

  // A judge should never meet an empty knowledge base: index the samples as soon as we know it is empty.
  React.useEffect(() => {
    if (!judge || autoLoaded.current || documentCount !== 0) return;
    autoLoaded.current = true;
    void loadSample();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [judge, documentCount]);

  // Resume a meeting that is already running (a refresh must not lose it).
  React.useEffect(() => {
    let cancelled = false;
    api
      .playgroundSession()
      .then((current) => {
        if (cancelled) return;
        if (current) apply(current);
        else setSession(current);
      })
      .catch(() => undefined)
      .finally(() => {
        if (!cancelled) setLoadingSession(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // The delegate's follow-up after an approval, and a freshly raised approval,
  // arrive off the request path; poll while the meeting is on.
  React.useEffect(() => {
    if (!live) return;
    const id = window.setInterval(() => {
      if (busyRef.current !== null || document.visibilityState !== "visible") return;
      api
        .playgroundSession()
        .then((next) => {
          if (busyRef.current === null && next !== null) apply(next);
        })
        .catch(() => undefined);
    }, 1500);
    return () => window.clearInterval(id);
  }, [live]);

  const eventCount = session?.events.length ?? 0;
  React.useEffect(() => {
    const el = feedRef.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [eventCount]);

  async function start(participants: string[] = DEFAULT_PARTICIPANTS): Promise<PlaygroundSession | null> {
    setBusy("start");
    try {
      voice.cancel();
      const next = await api.playgroundStart(participants);
      spokenUpTo.current = Math.max(-1, ...next.events.map((e) => e.seq));
      liveSpoken.current = [];
      setSession(next);
      setResults({});
      setSelected(null);
      setCursor(0);
      if (participants[0]) setSpeaker(participants[0]);
      return next;
    } catch (error) {
      toast.push({ title: "Could not start the meeting", description: errorMessage(error), variant: "error" });
      return null;
    } finally {
      setBusy(null);
    }
  }

  async function say(who: string, line: string): Promise<boolean> {
    const clean = line.trim();
    if (!clean) return false;
    setBusy("say");
    try {
      // The room hears the line while the delegate is still working out its answer.
      liveSpoken.current.push(clean);
      void voice.speak("human", who, clean);
      const out = await api.playgroundSay(who, clean);
      apply(out.session);
      const utterance = [...out.session.events].reverse().find((e) => e.kind === "utterance" && e.text === clean);
      if (utterance) {
        setResults((prev) => ({ ...prev, [utterance.seq]: { ...out.result, latency_ms: out.latency_ms } }));
        setSelected(utterance.seq);
      }
      return true;
    } catch (error) {
      toast.push({ title: "The delegate could not handle that line", description: errorMessage(error), variant: "error" });
      return false;
    } finally {
      setBusy(null);
    }
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!session || busy !== null) return;
    const line = text;
    setText("");
    const ok = await say(speaker || "Guest", line);
    if (!ok) setText(line);
  }

  async function resolve(approval: PlaygroundApproval, outcome: "APPROVED" | "REJECTED" | "DECLINED_POLITELY") {
    setBusy(`resolve:${approval.request_id}`);
    try {
      apply(await api.playgroundResolve(approval.request_id, outcome));
    } catch (error) {
      toast.push({ title: "Could not answer", description: errorMessage(error), variant: "error" });
    } finally {
      setBusy(null);
    }
  }

  async function conclude() {
    setBusy("conclude");
    stopRef.current = true;
    voice.cancel();
    try {
      setSession(await api.playgroundConclude());
    } catch (error) {
      toast.push({ title: "Could not end the meeting", description: errorMessage(error), variant: "error" });
    } finally {
      setBusy(null);
    }
  }

  async function loadSample() {
    setBusy("sample");
    try {
      const out = await api.playgroundSampleData();
      await knowledge.refresh();
      toast.push({
        title: out.loaded.length ? "Sample documents added" : "Sample documents were already added",
        description: out.loaded.length ? `${out.loaded.length} documents are ready for your delegate to answer from.` : undefined,
      });
    } catch (error) {
      toast.push({ title: "Could not load the sample data", description: errorMessage(error), variant: "error" });
    } finally {
      setBusy(null);
    }
  }

  async function discard() {
    stopRef.current = true;
    voice.cancel();
    spokenUpTo.current = null;
    try {
      await api.playgroundDiscard();
    } catch {
      /* already gone */
    }
    setSession(null);
    setResults({});
    setSelected(null);
    setScenario(null);
    setCursor(0);
    setRunning(false);
  }

  async function begin(chosen: Scenario, mode: "step" | "run") {
    stopRef.current = false;
    setScenario(chosen);
    const started = await start(chosen.participants);
    if (!started) return;
    if (mode === "run") await play(chosen, 0);
  }

  async function play(chosen: Scenario, from: number) {
    setRunning(true);
    stopRef.current = false;
    try {
      for (let index = from; index < chosen.turns.length; index += 1) {
        // A risky line leaves an approval open; the demo waits for a human.
        let held = false;
        while (pendingRef.current > 0 && !stopRef.current) {
          held = true;
          await sleep(400);
        }
        if (stopRef.current) break;
        // After an approval, let the delegate finish its follow-up before the next person speaks.
        if (held) await sleep(600);
        await voice.idle();
        if (stopRef.current) break;
        const turn = chosen.turns[index];
        const ok = await say(turn.speaker, turn.text);
        if (!ok) break;
        setCursor(index + 1);
        await voice.idle();
        await sleep(500);
      }
    } finally {
      setRunning(false);
    }
  }

  async function sendNext() {
    if (!scenario || cursor >= scenario.turns.length || busy !== null) return;
    const turn = scenario.turns[cursor];
    const ok = await say(turn.speaker, turn.text);
    if (ok) setCursor(cursor + 1);
  }

  const utteranceCount = session?.events.filter((e) => e.kind === "utterance").length ?? 0;
  const scenarioDone = scenario !== null && cursor >= scenario.turns.length;
  // The moment to wrap up: nobody is waiting on you, nothing is running, and the conversation has run its course.
  const readyToEnd = live && pending.length === 0 && busy === null && !running && (scenarioDone || utteranceCount >= 4);

  const selectedResult = selected !== null ? results[selected] : undefined;
  const selectedEvent = session?.events.find((e) => e.seq === selected);

  return (
    <div className="space-y-5">
      <PageHeader
        title="Playground"
        description="Run a whole meeting right here: no microphone, no meeting link, no phone. Every line is handled just as it would be in a real meeting, using your own documents."
        actions={
          <>
            {voice.supported ? (
              <Button variant="outline" size="sm" onClick={voice.toggle} aria-pressed={voice.enabled} title="Speak the meeting aloud with two voices">
                {voice.enabled ? <Volume2 className="size-3.5" /> : <VolumeX className="size-3.5" />}
                Voice {voice.enabled ? "on" : "off"}
              </Button>
            ) : null}
            {session && tech ? <Badge variant="info" dot>Powered by {providerOf(session.model)}</Badge> : null}
            {documentCount === 0 ? (
              <Button size="sm" onClick={loadSample} loading={busy === "sample"}>
                <Sparkles className="size-3.5" /> Load sample data
              </Button>
            ) : null}
            {session ? (
              <Button variant="outline" size="sm" onClick={discard} disabled={busy !== null}>
                <RotateCcw className="size-3.5" /> Reset
              </Button>
            ) : null}
          </>
        }
      />

      {judge ? (
        <JudgeBanner
          session={session}
          ready={documentCount !== null && documentCount > 0}
          loading={busy === "sample" || documentCount === 0}
          busy={busy !== null}
          negotiation={scenarios.data?.find((s) => s.key === "negotiation")}
          onStart={(s) => begin(s, "run")}
        />
      ) : null}

      {loadingSession ? (
        <Skeleton className="h-96 w-full" />
      ) : session === null ? (
        <Launcher
          scenarios={scenarios.data}
          scenariosError={scenarios.error}
          documentCount={documentCount}
          busy={busy}
          onLoadSample={loadSample}
          onBegin={begin}
          onBlank={() => start()}
        />
      ) : (
        <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_380px]">
          <div className="space-y-5">
            {scenario ? (
              <ScenarioStrip
                scenario={scenario}
                cursor={cursor}
                running={running}
                disabled={busy !== null || !live}
                onNext={sendNext}
                onRun={() => play(scenario, cursor)}
                onStop={() => {
                  stopRef.current = true;
                  voice.cancel();
                }}
              />
            ) : null}

            <Card className="flex h-[560px] flex-col">
              <CardHeader className="flex-row items-center justify-between gap-3 pb-3">
                <div>
                  <CardTitle>Meeting</CardTitle>
                  <CardDescription>
                    {session.principal}&apos;s delegate is attending
                    {session.participants.length ? ` with ${session.participants.join(", ")}` : ""}. Click a line to see how it was decided.
                  </CardDescription>
                </div>
                {voice.speaking ? (
                  <Badge variant={voice.speaking.role === "delegate" ? "info" : "secondary"} dot>
                    <Volume2 className="size-3" aria-hidden="true" /> {voice.speaking.name} speaking
                  </Badge>
                ) : null}
                {live ? (
                  <EndMeetingButton ready={readyToEnd} concluding={busy === "conclude"} disabled={busy !== null && busy !== "conclude"} onClick={conclude} />
                ) : (
                  <Badge variant="secondary">Ended</Badge>
                )}
              </CardHeader>
              <div ref={feedRef} className="flex-1 space-y-3 overflow-y-auto px-5 pb-3" aria-live="polite">
                {session.events.map((event) => (
                  <FeedItem
                    key={event.seq}
                    event={event}
                    chosen={event.seq === selected}
                    inspectable={event.kind === "utterance" && results[event.seq] !== undefined}
                    waiting={pending.some((a) => a.request_id === event.request_id)}
                    onSelect={() => setSelected(event.seq)}
                  />
                ))}
                {readyToEnd ? (
                  <div className="fade-in mx-auto flex w-full max-w-md items-center gap-3 rounded-xl border border-primary/40 bg-primary/10 px-4 py-3">
                    <FileText className="size-4 shrink-0 text-primary" aria-hidden="true" />
                    <p className="min-w-0 flex-1 text-sm">
                      <span className="font-semibold">That&apos;s a wrap.</span> End the meeting to get your briefing.
                    </p>
                    <Button size="sm" onClick={conclude}>
                      Brief me
                    </Button>
                  </div>
                ) : null}
                {busy === "say" ? (
                  <div className="flex items-center gap-2 text-xs text-muted-foreground">
                    <CircleDot className="size-3.5 animate-pulse text-primary" /> {session.principal}&apos;s delegate is thinking…
                  </div>
                ) : null}
              </div>
              <form onSubmit={submit} className="flex items-center gap-2 border-t border-border p-3">
                <Input
                  value={speaker}
                  onChange={(e) => setSpeaker(e.target.value)}
                  className="w-28 shrink-0"
                  aria-label="Who is speaking"
                  placeholder="Speaker"
                  maxLength={80}
                  disabled={!live}
                />
                <Input
                  value={text}
                  onChange={(e) => setText(e.target.value)}
                  aria-label="What they say"
                  placeholder={live ? "Say something into the meeting…" : "This meeting has ended"}
                  maxLength={1500}
                  disabled={!live}
                />
                <Button type="submit" size="md" disabled={!live || busy !== null || !text.trim()} loading={busy === "say"}>
                  <Send className="size-4" /> Send
                </Button>
              </form>
            </Card>

            {session.concluded && session.report ? <BriefingCard session={session} model={tech ? providerOf(session.report.summariser) : null} /> : null}
          </div>

          <div className="space-y-5 lg:sticky lg:top-4 lg:max-h-[calc(100vh-2rem)] lg:self-start lg:overflow-y-auto lg:pr-1">
            <PhonePanel
              session={session}
              pending={pending}
              busy={busy}
              onResolve={resolve}
              live={live}
            />
            <Inspector result={selectedResult} event={selectedEvent} model={session.model} tech={tech} onTech={setTech} />
            <ActionItems session={session} />
          </div>
        </div>
      )}
    </div>
  );
}

function EndMeetingButton({
  ready,
  concluding,
  disabled,
  onClick,
}: {
  ready: boolean;
  concluding: boolean;
  disabled: boolean;
  onClick: () => void;
}) {
  return (
    <div className="flex items-center gap-3">
      {ready && !concluding ? (
        <span className="hidden text-xs font-medium text-primary sm:inline">Done talking? Get your briefing →</span>
      ) : null}
      <Button
        size={ready || concluding ? "md" : "sm"}
        variant={ready || concluding ? "default" : "outline"}
        onClick={onClick}
        loading={concluding}
        disabled={disabled}
        className={cn(ready && !concluding && "deleg8-nudge")}
      >
        {concluding ? (
          "Writing your briefing…"
        ) : (
          <>
            <FileText className="size-3.5" /> End &amp; brief me
          </>
        )}
      </Button>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Judge mode
// --------------------------------------------------------------------------- //

/** The shortest path through the product, with progress derived from what actually happened. */
function JudgeBanner({
  session,
  ready,
  loading,
  busy,
  negotiation,
  onStart,
}: {
  session: PlaygroundSession | null;
  ready: boolean;
  loading: boolean;
  busy: boolean;
  negotiation: Scenario | undefined;
  onStart: (scenario: Scenario) => void;
}) {
  const asked = (session?.events.filter((e) => e.kind === "utterance").length ?? 0) > 0;
  const decided = session?.approvals.some((a) => !a.pending && a.outcome) ?? false;
  const pendingNow = session?.approvals.some((a) => a.pending) ?? false;
  const briefed = session?.concluded === true && session.report !== null;
  const steps: { label: string; done: boolean }[] = [
    { label: "Knowledge ready", done: ready },
    { label: "Meeting played", done: asked },
    { label: "Approved from the phone", done: decided },
    { label: "Briefing read", done: briefed },
  ];
  const next = !ready
    ? "Indexing your sample documents…"
    : !session
      ? "Press the button: a customer will ask for a discount and the delegate must not improvise."
      : pendingNow
        ? "It is waiting for you. Approve, decline or soften on the phone panel (right)."
        : !decided && asked
          ? "Click any line in the transcript to see exactly why the delegate did what it did."
          : !briefed
            ? "Press “End & brief me” to get the briefing and the action items."
            : "Done. Now try to break it: run “Try to make it hallucinate”, or type your own lines.";
  const current = steps.findIndex((step) => !step.done);
  return (
    <Card className="border-primary/30 bg-primary/5">
      <CardContent className="space-y-4 p-5">
        <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div className="space-y-1">
            <p className="flex items-center gap-1.5 font-mono text-[10.5px] uppercase tracking-[0.12em] text-primary">
              <Sparkles className="size-3" aria-hidden="true" /> Judge mode
            </p>
            <p className="text-sm font-medium">{next}</p>
          </div>
          {!session && negotiation ? (
            <Button size="lg" onClick={() => onStart(negotiation)} disabled={busy || loading || !ready}>
              <Play className="size-4" /> Run the demo meeting
            </Button>
          ) : null}
        </div>
        <ol className="grid gap-2 sm:grid-cols-4">
          {steps.map((step, index) => (
            <li
              key={step.label}
              className={cn(
                "flex items-center gap-2.5 rounded-lg border px-3 py-2 text-xs",
                step.done
                  ? "border-success/30 bg-success/10 text-success"
                  : index === current
                    ? "border-primary/50 bg-card font-medium text-foreground"
                    : "border-border bg-card/50 text-muted-foreground",
              )}
            >
              <span
                className={cn(
                  "grid size-5 shrink-0 place-items-center rounded-full text-[10px] font-bold",
                  step.done ? "bg-success text-white" : index === current ? "bg-primary text-primary-foreground" : "bg-muted text-muted-foreground",
                )}
              >
                {step.done ? <Check className="size-3" aria-hidden="true" /> : index + 1}
              </span>
              {step.label}
            </li>
          ))}
        </ol>
      </CardContent>
    </Card>
  );
}

// --------------------------------------------------------------------------- //
// Launcher
// --------------------------------------------------------------------------- //

function Launcher({
  scenarios,
  scenariosError,
  documentCount,
  busy,
  onLoadSample,
  onBegin,
  onBlank,
}: {
  scenarios: Scenario[] | undefined;
  scenariosError: string | null;
  documentCount: number | null;
  busy: Busy;
  onLoadSample: () => void;
  onBegin: (scenario: Scenario, mode: "step" | "run") => void;
  onBlank: () => void;
}) {
  const needsSample = documentCount === 0;
  return (
    <div className="space-y-5">
      <Card>
        <CardContent className="grid gap-5 p-6 md:grid-cols-3">
          <Step
            n={1}
            title="Give it something to know"
            body={
              needsSample
                ? "Your account has no documents yet. Load a sample knowledge base - a profile, a project and a team page - or upload your own on the Knowledge page."
                : "Your documents are indexed. The delegate answers from them, and from nothing else."
            }
            action={
              needsSample ? (
                <Button size="sm" onClick={onLoadSample} loading={busy === "sample"}>
                  <Sparkles className="size-3.5" /> Load sample data
                </Button>
              ) : (
                <Badge variant="success" dot>{documentCount ?? "…"} documents ready</Badge>
              )
            }
          />
          <Step
            n={2}
            title="Say something into a meeting"
            body="Play a scripted meeting below, or start an empty one and type as any participant. Try to trip it up."
          />
          <Step
            n={3}
            title="Watch it decide"
            body="It answers what your documents support, stays quiet when a line is not for it, and asks your phone before it touches money, dates or anything personal."
          />
        </CardContent>
      </Card>

      <div className="grid gap-4 md:grid-cols-3">
        {scenarios === undefined ? (
          scenariosError ? (
            <p className="col-span-full text-sm text-danger">{scenariosError}</p>
          ) : (
            [0, 1, 2].map((i) => <Skeleton key={i} className="h-52" />)
          )
        ) : (
          scenarios.map((scenario) => (
            <Card key={scenario.key} className="flex flex-col">
              <CardHeader>
                <CardTitle>{scenario.title}</CardTitle>
                <CardDescription>{scenario.summary}</CardDescription>
              </CardHeader>
              <CardContent className="mt-auto flex flex-wrap gap-2">
                <Button size="sm" onClick={() => onBegin(scenario, "run")} disabled={busy !== null || needsSample}>
                  <Play className="size-3.5" /> Run it
                </Button>
                <Button size="sm" variant="outline" onClick={() => onBegin(scenario, "step")} disabled={busy !== null || needsSample}>
                  Step through
                </Button>
                <span className="self-center text-xs text-muted-foreground">{scenario.turns.length} lines</span>
              </CardContent>
            </Card>
          ))
        )}
      </div>

      <div className="flex items-center gap-3">
        <Button variant="outline" onClick={onBlank} disabled={busy !== null}>
          <Bot className="size-4" /> Start an empty meeting
        </Button>
        <p className="text-xs text-muted-foreground">Type as anyone. Nothing leaves this page: no call is joined and no message is sent.</p>
      </div>
    </div>
  );
}

function Step({ n, title, body, action }: { n: number; title: string; body: string; action?: React.ReactNode }) {
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <span className="flex size-6 items-center justify-center rounded-full bg-primary text-xs font-semibold text-primary-foreground">{n}</span>
        <h3 className="text-sm font-semibold">{title}</h3>
      </div>
      <p className="text-sm text-muted-foreground">{body}</p>
      {action}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Scenario strip
// --------------------------------------------------------------------------- //

function ScenarioStrip({
  scenario,
  cursor,
  running,
  disabled,
  onNext,
  onRun,
  onStop,
}: {
  scenario: Scenario;
  cursor: number;
  running: boolean;
  disabled: boolean;
  onNext: () => void;
  onRun: () => void;
  onStop: () => void;
}) {
  const done = cursor >= scenario.turns.length;
  const next = done ? null : scenario.turns[cursor];
  return (
    <Card>
      <CardContent className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold">{scenario.title}</span>
            <Badge variant="outline">
              {Math.min(cursor, scenario.turns.length)} / {scenario.turns.length}
            </Badge>
          </div>
          {next ? (
            <p className="mt-1 truncate text-xs text-muted-foreground">
              Next - <span className="font-medium text-foreground">{next.speaker}:</span> {next.text}
            </p>
          ) : (
            <p className="mt-1 text-xs text-muted-foreground">Scenario finished. Keep talking, or end the meeting for the briefing.</p>
          )}
          {next ? <p className="mt-0.5 text-xs text-success">Expect: {next.expect}</p> : null}
        </div>
        <div className="flex shrink-0 gap-2">
          {running ? (
            <Button size="sm" variant="outline" onClick={onStop}>
              <Square className="size-3.5" /> Stop
            </Button>
          ) : (
            <>
              <Button size="sm" variant="outline" onClick={onNext} disabled={disabled || done}>
                Send next line
              </Button>
              <Button size="sm" onClick={onRun} disabled={disabled || done}>
                <Play className="size-3.5" /> Run the rest
              </Button>
            </>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

// --------------------------------------------------------------------------- //
// Feed
// --------------------------------------------------------------------------- //

function Avatar({ name, bot }: { name: string; bot?: boolean }) {
  return (
    <span
      className={cn(
        "grid size-7 shrink-0 place-items-center rounded-full text-[11px] font-semibold",
        bot ? "bg-primary text-primary-foreground" : "bg-muted text-muted-foreground ring-1 ring-border",
      )}
      aria-hidden="true"
    >
      {bot ? <Bot className="size-3.5" /> : name.slice(0, 1).toUpperCase()}
    </span>
  );
}

function FeedItem({
  event,
  chosen,
  inspectable,
  waiting,
  onSelect,
}: {
  event: PlaygroundEvent;
  chosen: boolean;
  inspectable: boolean;
  waiting: boolean;
  onSelect: () => void;
}) {
  const [tech] = useTechDetails();
  switch (event.kind) {
    case "system":
      return (
        <p className="mx-auto w-fit rounded-full bg-muted px-3 py-1 text-center text-[11px] text-muted-foreground">{event.text}</p>
      );
    case "utterance":
      return (
        <div className="flex max-w-[82%] items-end gap-2">
          <Avatar name={event.speaker ?? "?"} />
          <div className="flex min-w-0 flex-col items-start gap-1">
            <span className="px-1 text-[11px] font-medium text-muted-foreground">{event.speaker}</span>
            <button
              type="button"
              onClick={onSelect}
              disabled={!inspectable}
              title={inspectable ? "See why the delegate did what it did" : undefined}
              className={cn(
                "rounded-2xl rounded-bl-sm border bg-muted px-3.5 py-2 text-left text-sm leading-relaxed transition-colors",
                inspectable && "cursor-pointer hover:bg-accent",
                chosen ? "border-primary ring-1 ring-primary/40" : "border-transparent",
              )}
            >
              {event.text}
            </button>
          </div>
        </div>
      );
    case "delegate":
      return (
        <div className="ml-auto flex max-w-[82%] items-end gap-2">
          <div className="flex min-w-0 flex-col items-end gap-1">
            <span className="flex flex-wrap items-center justify-end gap-1.5 px-1 text-[11px] font-medium text-muted-foreground">
              {event.followup ? <Badge variant="violet">After your decision</Badge> : null}
              {event.state ? <DecisionBadge state={event.state} /> : null}
              Delegate{tech && event.latency_ms ? ` · ${(event.latency_ms / 1000).toFixed(1)}s` : ""}
            </span>
            <div className="rounded-2xl rounded-br-sm bg-primary px-3.5 py-2 text-sm leading-relaxed text-primary-foreground">{event.text}</div>
          </div>
          <Avatar name="Delegate" bot />
        </div>
      );
    case "silent":
      return (
        <p className="flex items-center justify-center gap-1.5 text-center text-xs italic text-muted-foreground">
          <Ban className="size-3" aria-hidden="true" /> The delegate stayed silent{event.reason ? ` - ${event.reason}` : ""}
        </p>
      );
    case "approval_requested":
      return waiting ? (
        <button
          type="button"
          onClick={() => document.getElementById("phone-panel")?.scrollIntoView({ behavior: "smooth", block: "center" })}
          className="mx-auto flex w-full max-w-md items-center gap-3 rounded-xl border border-warning bg-warning/10 px-3.5 py-2.5 text-left transition-colors hover:bg-warning/20"
        >
          <Smartphone className="size-4 shrink-0 text-warning-foreground" aria-hidden="true" />
          <span className="min-w-0 flex-1">
            <span className="block text-sm font-semibold text-warning-foreground">Waiting for your approval</span>
            <span className="block truncate text-xs text-warning-foreground/80">
              {event.topic ? `${event.topic.replaceAll("_", " ")} - ` : ""}answer on the phone
            </span>
          </span>
          <ArrowRight className="size-4 shrink-0 text-warning-foreground" aria-hidden="true" />
        </button>
      ) : (
        <p className="flex items-center justify-center gap-1.5 text-center text-xs text-muted-foreground">
          <Smartphone className="size-3" aria-hidden="true" /> Asked your phone{event.topic ? ` · ${event.topic.replaceAll("_", " ")}` : ""}
        </p>
      );
    case "approval_resolved":
      return (
        <p className="mx-auto flex w-fit items-center justify-center gap-1.5 rounded-full bg-success/10 px-3 py-1 text-center text-xs font-medium text-success">
          <Check className="size-3" aria-hidden="true" /> You chose {event.outcome?.toLowerCase().replaceAll("_", " ")}
        </p>
      );
    default:
      return null;
  }
}

// --------------------------------------------------------------------------- //
// Phone
// --------------------------------------------------------------------------- //

type Outcome = "APPROVED" | "REJECTED" | "DECLINED_POLITELY";

const OUTCOME_LABEL: Record<string, string> = {
  APPROVED: "Approved",
  REJECTED: "Rejected",
  DECLINED_POLITELY: "Declined politely",
};

function PhonePanel({
  session,
  pending,
  busy,
  onResolve,
  live,
}: {
  session: PlaygroundSession;
  pending: PlaygroundApproval[];
  busy: Busy;
  onResolve: (approval: PlaygroundApproval, outcome: Outcome) => void;
  live: boolean;
}) {
  const waiting = pending.length > 0;
  const chatRef = React.useRef<HTMLDivElement>(null);
  const count = session.approvals.length + (waiting ? 1 : 0);
  React.useEffect(() => {
    chatRef.current?.scrollTo({ top: chatRef.current.scrollHeight, behavior: "smooth" });
  }, [count]);

  return (
    <section id="phone-panel" aria-label="Your phone" className="space-y-3">
      {waiting ? (
        <div role="alert" className="flex items-center gap-3 rounded-xl border border-warning bg-warning/15 px-4 py-3">
          <span className="relative flex size-3 shrink-0">
            <span className="absolute inline-flex size-full animate-ping rounded-full bg-warning opacity-75" />
            <span className="relative inline-flex size-3 rounded-full bg-warning" />
          </span>
          <div>
            <p className="text-sm font-semibold text-warning-foreground">Your approval is needed</p>
            <p className="text-xs text-warning-foreground/80">The delegate is holding the line. Tap a button on the phone below.</p>
          </div>
        </div>
      ) : null}

      <div
        className={cn(
          "mx-auto w-full max-w-[340px] rounded-[2.4rem] bg-zinc-800 p-2 shadow-[0_24px_50px_-20px_rgba(0,0,0,0.55)] ring-1 ring-black/30 transition-shadow",
          waiting && "deleg8-buzz ring-2 ring-warning shadow-[0_0_0_6px_color-mix(in_oklch,var(--warning)_25%,transparent),0_24px_50px_-20px_rgba(0,0,0,0.55)]",
        )}
      >
        <div className="relative overflow-hidden rounded-[2rem] bg-zinc-950 text-zinc-100">
          <div className="flex items-center justify-between px-6 pt-3 text-[10px] font-semibold text-zinc-400">
            <span>9:41</span>
            <span className="absolute left-1/2 top-2.5 h-[18px] w-20 -translate-x-1/2 rounded-full bg-black" aria-hidden="true" />
            <span>5G</span>
          </div>

          <div className="mt-2 flex items-center gap-2.5 border-b border-white/10 px-4 pb-3">
            <span className="grid size-8 place-items-center rounded-full bg-primary text-[11px] font-bold text-primary-foreground">OO</span>
            <div className="min-w-0 leading-tight">
              <p className="text-sm font-semibold">Out of Office bot</p>
              <p className="text-[11px] text-zinc-400">{waiting ? "needs your decision" : live ? "listening to the meeting" : "meeting ended"}</p>
            </div>
            {waiting ? (
              <span className="ml-auto grid size-5 place-items-center rounded-full bg-warning text-[11px] font-bold text-black">{pending.length}</span>
            ) : null}
          </div>

          <div ref={chatRef} className="h-[400px] space-y-3 overflow-y-auto px-3 py-3">
            {session.approvals.length === 0 ? (
              <div className="grid h-full place-items-center px-4 text-center">
                <div className="space-y-2">
                  <Smartphone className="mx-auto size-7 text-zinc-600" aria-hidden="true" />
                  <p className="text-sm font-medium text-zinc-300">No messages yet</p>
                  <p className="text-xs leading-relaxed text-zinc-500">
                    {live
                      ? "When someone asks about money, dates or anything personal, the delegate messages you here and waits for your tap."
                      : "The meeting has ended."}
                  </p>
                </div>
              </div>
            ) : (
              session.approvals.map((approval) => (
                <PhoneRequest
                  key={approval.request_id}
                  approval={approval}
                  busy={busy === `resolve:${approval.request_id}`}
                  locked={busy !== null}
                  onResolve={onResolve}
                />
              ))
            )}
          </div>
          <div className="mx-auto mb-2 h-1 w-24 rounded-full bg-zinc-700" aria-hidden="true" />
        </div>
      </div>
      <p className="px-2 text-center text-[11px] leading-relaxed text-muted-foreground">
        In a real meeting this is a Telegram message. The delegate has already told the room it will come back to them.
      </p>
    </section>
  );
}

function PhoneRequest({
  approval,
  busy,
  locked,
  onResolve,
}: {
  approval: PlaygroundApproval;
  busy: boolean;
  locked: boolean;
  onResolve: (approval: PlaygroundApproval, outcome: Outcome) => void;
}) {
  const choices = approval.choices;
  const topic = approval.topic?.replaceAll("_", " ") ?? "needs you";
  return (
    <div className="space-y-2">
      <div className="max-w-[92%] space-y-2 rounded-2xl rounded-tl-sm bg-zinc-800 px-3.5 py-3">
        <div className="flex items-center gap-2">
          {/* The badge colours are tuned for a light page; on the dark phone they need solid fills to stay readable. */}
          <TierBadge
            tier={approval.tier}
            className={cn(
              approval.tier === "GREEN" && "!border-emerald-400 !bg-emerald-400 !text-zinc-900",
              approval.tier === "YELLOW" && "!border-amber-300 !bg-amber-300 !text-zinc-900",
              approval.tier === "RED" && "!border-red-400 !bg-red-400 !text-zinc-900",
            )}
          />
          <span className="text-[11px] font-semibold uppercase tracking-wider text-zinc-400">{topic}</span>
        </div>
        <p className="text-sm leading-snug">
          <span className="font-semibold">{approval.speaker ?? "Someone"}</span> asked:
          <span className="mt-1 block text-zinc-300">&ldquo;{approval.heard}&rdquo;</span>
        </p>
        {approval.pending && approval.reason ? <p className="text-xs leading-relaxed text-zinc-400">{approval.reason}</p> : null}
        {approval.pending ? <p className="text-xs font-medium text-amber-300">Choose what I should say:</p> : null}
      </div>

      {approval.pending ? (
        <div className="space-y-1.5">
          <PhoneButton
            tone="approve"
            icon={<Check className="size-4" />}
            label="Approve"
            say={choices?.approve ?? approval.proposed}
            onClick={() => onResolve(approval, "APPROVED")}
            disabled={locked}
            loading={busy}
          />
          <PhoneButton
            tone="neutral"
            icon={<ShieldCheck className="size-4" />}
            label="Decline politely"
            say={choices?.soften ?? ""}
            onClick={() => onResolve(approval, "DECLINED_POLITELY")}
            disabled={locked}
          />
          <PhoneButton
            tone="reject"
            icon={<X className="size-4" />}
            label="Reject"
            say={choices?.reject ?? ""}
            onClick={() => onResolve(approval, "REJECTED")}
            disabled={locked}
          />
        </div>
      ) : (
        <div className="flex justify-end">
          <span
            className={cn(
              "inline-flex items-center gap-1.5 rounded-2xl rounded-tr-sm px-3 py-1.5 text-xs font-medium",
              approval.outcome === "APPROVED" ? "bg-emerald-600 text-white" : "bg-zinc-700 text-zinc-100",
            )}
          >
            <Check className="size-3.5" aria-hidden="true" /> {OUTCOME_LABEL[approval.outcome ?? ""] ?? approval.outcome ?? "Answered"}
          </span>
        </div>
      )}
    </div>
  );
}

function PhoneButton({
  tone,
  icon,
  label,
  say,
  onClick,
  disabled,
  loading,
}: {
  tone: "approve" | "neutral" | "reject";
  icon: React.ReactNode;
  label: string;
  say: string;
  onClick: () => void;
  disabled: boolean;
  loading?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "flex w-full items-start gap-2.5 rounded-xl px-3.5 py-2.5 text-left transition-[filter,opacity] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/70 enabled:hover:brightness-110 disabled:opacity-50",
        tone === "approve" && "bg-emerald-600 text-white",
        tone === "neutral" && "bg-zinc-700 text-zinc-50",
        tone === "reject" && "bg-zinc-800 text-red-300 ring-1 ring-inset ring-red-400/30",
      )}
    >
      <span className="mt-0.5 shrink-0">{loading ? <Loader2 className="size-4 animate-spin" aria-hidden="true" /> : icon}</span>
      <span className="min-w-0">
        <span className="block text-sm font-semibold leading-tight">{label}</span>
        {say ? <span className="mt-0.5 block text-[11px] leading-snug opacity-80">Says: &ldquo;{say}&rdquo;</span> : null}
      </span>
    </button>
  );
}

// --------------------------------------------------------------------------- //
// Inspector
// --------------------------------------------------------------------------- //

function plainHeadline(result: PlaygroundTurnResult): string {
  const topic = result.decision.topic?.replaceAll("_", " ");
  if (result.state === "AUTO_REPLY") return "It answered because your documents had the answer.";
  if (result.state === "ESCALATE")
    return topic ? `It didn't answer on its own because this touches ${topic}. It asked you first.` : "It wasn't sure it should answer, so it asked you first.";
  return "It stayed quiet because this wasn't meant for your delegate.";
}

function Inspector({
  result,
  event,
  model,
  tech,
  onTech,
}: {
  result: PlaygroundTurnResult | undefined;
  event: PlaygroundEvent | undefined;
  model: string;
  tech: boolean;
  onTech: (on: boolean) => void;
}) {
  const labels = tech ? STAGE_LABEL : PLAIN_STAGE_LABEL;
  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <CardTitle className="flex items-center gap-2">
            <ShieldCheck className="size-4" /> Why it did that
          </CardTitle>
          <label className="flex shrink-0 cursor-pointer items-center gap-2 text-[11px] text-muted-foreground">
            Technical details
            <Switch checked={tech} onCheckedChange={onTech} aria-label="Show technical details" />
          </label>
        </div>
        <CardDescription>{tech ? "Every decision is traced. Nothing is a black box." : "Click anything someone said to see how your delegate decided."}</CardDescription>
      </CardHeader>
      <CardContent>
        {!result ? (
          <EmptyState compact title="Click anything someone said" description="to see what your delegate checked and why it answered, asked you, or stayed quiet." />
        ) : (
          <div className="space-y-4">
            {event?.text ? (
              <p className="flex gap-2 text-sm">
                <Quote className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
                <span className="italic">{event.text}</span>
              </p>
            ) : null}
            <p className="text-sm font-medium leading-snug">{plainHeadline(result)}</p>
            <div className="flex flex-wrap items-center gap-2">
              <DecisionBadge state={result.state} />
              <TierBadge tier={result.decision.tier} />
              {tech && result.decision.topic ? <Badge variant="outline">{result.decision.topic.replaceAll("_", " ")}</Badge> : null}
              {tech && result.turn_type && result.turn_type !== result.decision.topic ? <Badge variant="secondary">{result.turn_type}</Badge> : null}
              {tech && result.guard_verdict ? <Badge variant={result.guard_verdict.toUpperCase() === "PASSED" ? "success" : "warning"}>guard: {result.guard_verdict.toLowerCase()}</Badge> : null}
              {tech && result.latency_ms !== undefined ? <Badge variant="outline">{(result.latency_ms / 1000).toFixed(1)}s end to end</Badge> : null}
              {tech ? <Badge variant="info">{providerOf(model)}</Badge> : null}
            </div>
            {result.guard_verdict ? (
              <p className="rounded-md border border-border p-2.5 text-xs leading-relaxed text-muted-foreground">
                <strong className="text-foreground">{tech ? "Output guard: " : "Fact check: "}</strong>
                {GUARD_EXPLAIN[result.guard_verdict.toLowerCase()] ?? result.guard_verdict}
              </p>
            ) : null}
            <ol className="space-y-2.5 border-l border-border pl-4">
              {result.decision.trace.map((step, index) => (
                <li key={`${step.stage}-${index}`} className="relative">
                  <span className="absolute -left-[21px] top-1.5 size-2 rounded-full bg-primary" aria-hidden="true" />
                  {tech ? (
                    <>
                      <p className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">{labels[step.stage] ?? step.stage}</p>
                      <p className="text-xs leading-relaxed">{step.outcome}</p>
                    </>
                  ) : (
                    <p className="text-xs leading-relaxed">{labels[step.stage] ?? step.stage}</p>
                  )}
                </li>
              ))}
            </ol>
            {result.decision.grounding ? (
              <div className="space-y-1.5 rounded-md bg-muted/60 p-3">
                <p className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                  <FileText className="size-3" aria-hidden="true" /> {tech ? `Grounded in ${result.decision.grounding.source}` : "Where it found this"}
                  {tech && result.decision.grounding.similarity !== null ? ` · similarity ${result.decision.grounding.similarity.toFixed(2)}` : ""}
                </p>
                {result.decision.grounding.citations.length ? (
                  <ul className="space-y-0.5 text-xs">
                    {result.decision.grounding.citations.slice(0, 4).map((citation) => (
                      <li key={citation} className="truncate">
                        {citation}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-xs">{result.decision.grounding.subject}</p>
                )}
              </div>
            ) : null}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// --------------------------------------------------------------------------- //
// Action items and the briefing
// --------------------------------------------------------------------------- //

function ActionItems({ session }: { session: PlaygroundSession }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ListChecks className="size-4" /> Action items
        </CardTitle>
        <CardDescription>Assembled across utterances, acknowledged aloud, and delivered to you.</CardDescription>
      </CardHeader>
      <CardContent>
        {session.commitments.length === 0 ? (
          <p className="text-sm text-muted-foreground">Ask the delegate to do something - &ldquo;send me the report by Friday&rdquo; - and it lands here.</p>
        ) : (
          <ul className="space-y-2.5">
            {session.commitments.map((item) => (
              <li key={item.id} className="space-y-0.5 text-sm">
                <p className="font-medium">{item.description}</p>
                <p className="text-xs text-muted-foreground">
                  {item.owner}
                  {item.requested_by ? ` · asked by ${item.requested_by}` : ""}
                  {item.due_text || item.due_iso ? ` · due ${item.due_text || item.due_iso}` : ""}
                  {item.acknowledged ? " · acknowledged aloud" : ""}
                </p>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
