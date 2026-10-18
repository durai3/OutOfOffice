"use client";

import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { LIVE_FEED_URL } from "@/lib/config";
import { getAccessToken } from "@/lib/auth-store";
import type {
  ActionItem,
  AgreedDecision,
  DecisionState,
  DelegateStatus,
  EscalationOutcome,
  EscalationResult,
  Grounding,
  HelloPayload,
  LiveEvent,
  MeetingSnapshot,
  OpenQuestion,
  PolicyDecision,
  PolicyTier,
  PostMeetingReport,
  TurnUpdate,
} from "@/lib/types";

export type ConnectionState = "connecting" | "open" | "closed";

export interface TranscriptLine {
  id: string;
  speaker: string;
  text: string;
  at: string;
  isDelegate: boolean;
  pending: boolean;
  decision: DecisionState | null;
  topic: string | null;
  tier: PolicyTier | null;
  reason: string | null;
  grounding: Grounding | null;
  brainMs: number | null;
  escalation: EscalationResult | null;
  proposedResponse: string | null;
  interrupted: boolean;
  interruptedBot: boolean;
}

export interface EscalationEntry {
  id: string;
  at: string;
  transcript: string;
  speaker: string | null;
  topic: string | null;
  tier: PolicyTier | null;
  reason: string | null;
  outcome: EscalationOutcome | "PENDING";
  respondedBy: string | null;
  responseSeconds: number | null;
  response: string | null;
}

export interface TimelineEntry {
  id: string;
  at: string;
  kind: "phase" | "session" | "voice" | "meeting" | "speech";
  label: string;
  detail: string | null;
}

/**
 * Whether the delegate can actually hear the room.
 *
 * A dead virtual cable looks exactly like an empty meeting from here - no
 * transcript either way - so the backend measures the input signal and says
 * which it is. Without this, "Nothing heard yet" was the only symptom of a
 * delegate that was never going to hear a word.
 */
export interface AudioHealth {
  inputDevice: string | null;
  outputDevice: string | null;
  peak: number | null;
  heardAnything: boolean;
  deaf: boolean;
  /** Device the meeting app must select itself, when the OS refused it as default. */
  inAppMic: string | null;
  muted: boolean;
  problem: string | null;
  notes: string[];
}

export const initialAudioHealth: AudioHealth = {
  inputDevice: null,
  outputDevice: null,
  peak: null,
  heardAnything: false,
  deaf: false,
  inAppMic: null,
  muted: false,
  problem: null,
  notes: [],
};

export interface LiveState {
  connection: ConnectionState;
  audio: AudioHealth;
  status: DelegateStatus | null;
  meetingId: string | null;
  participants: string[];
  turns: TranscriptLine[];
  interim: string;
  interimSpeaker: string | null;
  decisions: AgreedDecision[];
  actions: ActionItem[];
  questions: OpenQuestion[];
  escalations: EscalationEntry[];
  timeline: TimelineEntry[];
  lastReport: PostMeetingReport | null;
  serverVersion: string | null;
  lastEventAt: string | null;
  eventCount: number;
}

type Action =
  | { type: "connection"; state: ConnectionState }
  | { type: "hello"; payload: HelloPayload }
  | { type: "event"; event: LiveEvent };

const MAX_LINES = 600;
const MAX_TIMELINE = 200;

export const initialLiveState: LiveState = {
  connection: "connecting",
  audio: initialAudioHealth,
  status: null,
  meetingId: null,
  participants: [],
  turns: [],
  interim: "",
  interimSpeaker: null,
  decisions: [],
  actions: [],
  questions: [],
  escalations: [],
  timeline: [],
  lastReport: null,
  serverVersion: null,
  lastEventAt: null,
  eventCount: 0,
};

const TRANSCRIPT_EVENTS = new Set(["utterance", "decision", "turn", "transcript.interim"]);

function str(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function cap<T>(items: T[], max: number): T[] {
  return items.length > max ? items.slice(items.length - max) : items;
}

function fromSnapshot(snapshot: MeetingSnapshot): Pick<LiveState, "meetingId" | "participants" | "turns" | "decisions" | "actions" | "questions"> {
  return {
    meetingId: snapshot.meeting_id,
    participants: snapshot.participants,
    turns: snapshot.transcript.map((turn) => ({
      id: `snap-${turn.index}`,
      speaker: turn.speaker,
      text: turn.text,
      at: turn.timestamp,
      isDelegate: turn.is_delegate,
      pending: false,
      decision: turn.decision,
      topic: turn.topic,
      tier: turn.tier,
      reason: null,
      grounding: null,
      brainMs: null,
      escalation: null,
      proposedResponse: null,
      interrupted: false,
      interruptedBot: false,
    })),
    decisions: snapshot.agreed_decisions,
    actions: snapshot.pending_actions,
    questions: snapshot.open_questions,
  };
}

function timeline(state: LiveState, event: LiveEvent, kind: TimelineEntry["kind"], label: string, detail: string | null = null): TimelineEntry[] {
  return cap([...state.timeline, { id: `${event.seq}-${event.type}`, at: event.at, kind, label, detail }], MAX_TIMELINE);
}

function updateLastPending(turns: TranscriptLine[], patch: (line: TranscriptLine) => TranscriptLine): TranscriptLine[] {
  for (let i = turns.length - 1; i >= 0; i -= 1) {
    if (!turns[i].isDelegate && turns[i].pending) {
      const next = turns.slice();
      next[i] = patch(turns[i]);
      return next;
    }
  }
  for (let i = turns.length - 1; i >= 0; i -= 1) {
    if (!turns[i].isDelegate) {
      const next = turns.slice();
      next[i] = patch(turns[i]);
      return next;
    }
  }
  return turns;
}

function applyEvent(state: LiveState, event: LiveEvent): LiveState {
  const p = event.payload as Record<string, unknown>;
  const base: LiveState = { ...state, lastEventAt: event.at, eventCount: state.eventCount + 1 };

  switch (event.type) {
    case "status":
      return { ...base, status: event.payload as unknown as DelegateStatus };

    case "transcript.interim":
      return { ...base, interim: str(p.text) ?? "", interimSpeaker: str(p.speaker) };

    case "audio.route": {
      const notes = Array.isArray(p.notes) ? p.notes.filter((n): n is string => typeof n === "string") : [];
      const deaf = Boolean(p.deaf);
      return {
        ...base,
        audio: {
          ...state.audio,
          inputDevice: str(p.input_name),
          outputDevice: str(p.output_name),
          deaf,
          inAppMic: str(p.in_app_mic),
          notes,
          problem: deaf
            ? `The delegate's input device (${str(p.input_name) ?? "unknown"}) carries no audio, so it cannot hear the meeting.`
            : state.audio.problem,
        },
        timeline: timeline(state, event, "voice", "Audio route checked", str(p.input_name) ? `hears via ${p.input_name}` : null),
      };
    }

    case "audio.level":
      return {
        ...base,
        audio: {
          ...state.audio,
          peak: num(p.peak),
          muted: Boolean(p.muted),
          heardAnything: Boolean(p.heard_anything),
          inputDevice: str(p.device) ?? state.audio.inputDevice,
        },
      };

    case "audio.input_alive":
      return { ...base, audio: { ...state.audio, heardAnything: true, deaf: false, problem: null } };

    case "audio.input_silent":
      return {
        ...base,
        audio: { ...state.audio, heardAnything: false, deaf: true, problem: str(p.detail) },
        timeline: timeline(state, event, "voice", "Input device is silent", str(p.device)),
      };

    case "audio.route_check_failed":
      return { ...base, audio: { ...state.audio, problem: `Audio route could not be checked: ${str(p.error) ?? "unknown error"}` } };

    case "session.started":
      return {
        ...base,
        meetingId: str(p.meeting_id),
        participants: [str(p.principal), str(p.speaker_label)].filter((x): x is string => Boolean(x)),
        audio: initialAudioHealth,
        turns: [],
        interim: "",
        interimSpeaker: null,
        decisions: [],
        actions: [],
        questions: [],
        escalations: [],
        lastReport: null,
        timeline: [{ id: `${event.seq}-${event.type}`, at: event.at, kind: "session", label: "Delegate dispatched", detail: str(p.url) }],
      };

    case "session.phase": {
      const phase = str(p.phase) ?? "?";
      return { ...base, timeline: timeline(state, event, "phase", phase.replace(/_/g, " "), str(p.reason)) };
    }

    case "voice.ready":
      return { ...base, timeline: timeline(state, event, "voice", "Voice loop ready", str(p.voice) ? `voice ${p.voice}` : null) };

    case "voice.stopped":
      return { ...base, timeline: timeline(state, event, "voice", "Voice loop stopped") };

    case "session.stopping":
      return { ...base, timeline: timeline(state, event, "session", "Recall requested", str(p.reason)) };

    case "session.finished": {
      const error = str(p.error);
      const exit = num(p.exit_code);
      return {
        ...base,
        interim: "",
        timeline: timeline(state, event, "session", error ? "Session ended with an error" : "Session ended", error ?? str(p.reason) ?? (exit !== null ? `exit ${exit}` : null)),
      };
    }

    case "meeting.concluded":
      return {
        ...base,
        lastReport: (p.report as PostMeetingReport) ?? null,
        timeline: timeline(state, event, "meeting", "Executive briefing produced", (p.report as PostMeetingReport | undefined)?.receipt?.delivered ? "delivered" : "not delivered"),
      };

    case "utterance": {
      const line: TranscriptLine = {
        id: `${event.seq}-utt`,
        speaker: str(p.speaker) ?? "Meeting",
        text: str(p.text) ?? "",
        at: str(p.timestamp) ?? event.at,
        isDelegate: false,
        pending: true,
        decision: null,
        topic: null,
        tier: null,
        reason: null,
        grounding: null,
        brainMs: null,
        escalation: null,
        proposedResponse: null,
        interrupted: false,
        interruptedBot: Boolean(p.interrupted_bot),
      };
      return { ...base, interim: "", turns: cap([...state.turns, line], MAX_LINES) };
    }

    case "decision": {
      const decision = p.decision as PolicyDecision | undefined;
      if (!decision) return base;
      return {
        ...base,
        turns: updateLastPending(state.turns, (line) => ({
          ...line,
          pending: false,
          decision: decision.state,
          topic: decision.topic,
          tier: decision.tier,
          reason: decision.reason,
          grounding: decision.grounding,
          brainMs: num(p.brain_ms),
        })),
      };
    }

    case "escalation.requested": {
      const entry: EscalationEntry = {
        id: `${event.seq}-esc`,
        at: event.at,
        transcript: str(p.transcript) ?? "",
        speaker: str(p.speaker),
        topic: str(p.topic),
        tier: (str(p.tier) as PolicyTier | null) ?? null,
        reason: str(p.reason),
        outcome: "PENDING",
        respondedBy: null,
        responseSeconds: null,
        response: null,
      };
      return { ...base, escalations: [...state.escalations, entry] };
    }

    case "turn": {
      const escalation = (p.escalation as EscalationResult | null) ?? null;
      const memory = (p.memory as TurnUpdate | null) ?? null;
      const response = str(p.response);
      let turns = updateLastPending(state.turns, (line) => ({
        ...line,
        pending: false,
        decision: line.decision ?? ((str(p.state) as DecisionState | null) ?? null),
        escalation,
        proposedResponse: str(p.proposed_response),
      }));
      if (response) {
        turns = cap(
          [
            ...turns,
            {
              id: `${event.seq}-bot`,
              speaker: "Out of Office",
              text: response,
              at: event.at,
              isDelegate: true,
              pending: false,
              decision: null,
              topic: null,
              tier: null,
              reason: null,
              grounding: null,
              brainMs: null,
              escalation: null,
              proposedResponse: null,
              interrupted: false,
              interruptedBot: false,
            },
          ],
          MAX_LINES,
        );
      }
      let escalations = state.escalations;
      if (escalation) {
        const idx = [...escalations].reverse().findIndex((e) => e.outcome === "PENDING");
        if (idx !== -1) {
          const real = escalations.length - 1 - idx;
          escalations = escalations.slice();
          escalations[real] = {
            ...escalations[real],
            outcome: escalation.outcome,
            respondedBy: escalation.responded_by,
            responseSeconds: escalation.response_seconds,
            response,
          };
        }
      }
      return {
        ...base,
        turns,
        escalations,
        decisions: memory ? [...state.decisions, ...memory.new_decisions] : state.decisions,
        actions: memory ? [...state.actions, ...memory.new_actions] : state.actions,
        questions: memory ? [...state.questions, ...memory.new_questions] : state.questions,
      };
    }

    case "speech.interrupted": {
      const turns = state.turns.slice();
      for (let i = turns.length - 1; i >= 0; i -= 1) {
        if (turns[i].isDelegate) {
          turns[i] = { ...turns[i], interrupted: true };
          break;
        }
      }
      return { ...base, turns, timeline: timeline(state, event, "speech", "Interrupted", str(p.reason)) };
    }

    case "speech.end": {
      if (!p.interrupted) return base;
      const turns = state.turns.slice();
      for (let i = turns.length - 1; i >= 0; i -= 1) {
        if (turns[i].isDelegate) {
          turns[i] = { ...turns[i], interrupted: true };
          break;
        }
      }
      return { ...base, turns };
    }

    default:
      return base;
  }
}

function reducer(state: LiveState, action: Action): LiveState {
  switch (action.type) {
    case "connection":
      return { ...state, connection: action.state };
    case "hello": {
      const { status, snapshot, backlog, server_version } = action.payload;
      let next: LiveState = {
        ...initialLiveState,
        connection: "open",
        status,
        serverVersion: server_version,
        meetingId: status.meeting_id,
      };
      if (snapshot) next = { ...next, ...fromSnapshot(snapshot) };
      for (const event of backlog) {
        if (snapshot && TRANSCRIPT_EVENTS.has(event.type)) continue;
        if (snapshot && event.type === "session.started") {
          next = { ...next, timeline: [{ id: `${event.seq}-${event.type}`, at: event.at, kind: "session", label: "Delegate dispatched", detail: str((event.payload as Record<string, unknown>).url) }] };
          continue;
        }
        next = applyEvent(next, event);
      }
      return { ...next, status };
    }
    case "event":
      return applyEvent(state, action.event);
    default:
      return state;
  }
}

/** Pure helper (also used by tests): fold a hello frame and/or events into a state. */
export function reduceLiveFeed(state: LiveState, frames: Array<{ type: "hello"; payload: HelloPayload } | LiveEvent>): LiveState {
  let next = state;
  for (const frame of frames) {
    next = frame.type === "hello" ? reducer(next, { type: "hello", payload: (frame as { payload: HelloPayload }).payload }) : reducer(next, { type: "event", event: frame as LiveEvent });
  }
  return next;
}

export function useLiveFeed(enabled = true): LiveState & { reconnect: () => void } {
  const [state, dispatch] = useReducer(reducer, initialLiveState);
  const [attempt, setAttempt] = useState(0);
  const socketRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let disposed = false;
    let retry: number | undefined;
    let heartbeat: number | undefined;
    let backoff = 1000;

    const connect = () => {
      if (disposed) return;
      dispatch({ type: "connection", state: "connecting" });
      // A browser cannot set headers on a WebSocket, so the token rides in the
      // query string. The server resolves it to one tenant and subscribes the
      // socket to that tenant's own feed - there is no shared hub to filter.
      const token = getAccessToken();
      const socket = new WebSocket(token ? `${LIVE_FEED_URL}?token=${encodeURIComponent(token)}` : LIVE_FEED_URL);
      socketRef.current = socket;
      socket.onopen = () => {
        backoff = 1000;
        dispatch({ type: "connection", state: "open" });
        heartbeat = window.setInterval(() => {
          if (socket.readyState === WebSocket.OPEN) socket.send("ping");
        }, 25_000);
      };
      socket.onmessage = (message) => {
        try {
          const frame = JSON.parse(String(message.data)) as LiveEvent & { type: string };
          if (frame.type === "pong") return;
          if (frame.type === "hello") dispatch({ type: "hello", payload: frame.payload as unknown as HelloPayload });
          else dispatch({ type: "event", event: frame });
        } catch {
          /* ignore malformed frames */
        }
      };
      socket.onerror = () => socket.close();
      socket.onclose = () => {
        if (heartbeat) window.clearInterval(heartbeat);
        dispatch({ type: "connection", state: "closed" });
        if (!disposed) {
          retry = window.setTimeout(connect, backoff);
          backoff = Math.min(backoff * 2, 10_000);
        }
      };
    };

    connect();
    return () => {
      disposed = true;
      if (retry) window.clearTimeout(retry);
      if (heartbeat) window.clearInterval(heartbeat);
      socketRef.current?.close();
      socketRef.current = null;
    };
  }, [enabled, attempt]);

  const reconnect = useCallback(() => setAttempt((n) => n + 1), []);
  return { ...state, reconnect };
}
