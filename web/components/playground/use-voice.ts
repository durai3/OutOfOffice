"use client";

import * as React from "react";

/**
 * Spoken meeting for the playground, using the browser's built-in speech
 * synthesis (no key, no network). Two distinct voices make it sound like a real
 * call: the people in the room share one voice family, the delegate has another.
 */

export type Role = "human" | "delegate";
export type Speaking = { role: Role; name: string } | null;

type Job = { role: Role; name: string; text: string; done: () => void };

const DELEGATE_PREFERRED = [
  "Samantha", "Google UK English Female", "Microsoft Aria", "Microsoft Jenny", "Karen", "Moira", "Serena", "Female",
];
const HUMAN_PREFERRED = [
  "Daniel", "Alex", "Google UK English Male", "Microsoft Guy", "Microsoft Ryan", "Fred", "Oliver", "Male",
];

function hash(value: string): number {
  let h = 0;
  for (let i = 0; i < value.length; i += 1) h = (h * 31 + value.charCodeAt(i)) >>> 0;
  return h;
}

function pickByName(voices: SpeechSynthesisVoice[], preferred: string[]): SpeechSynthesisVoice[] {
  const found: SpeechSynthesisVoice[] = [];
  for (const wanted of preferred) {
    const match = voices.find((v) => v.name.includes(wanted) && !found.includes(v));
    if (match) found.push(match);
  }
  return found;
}

export function useVoice() {
  const supported = typeof window !== "undefined" && "speechSynthesis" in window;
  const [enabled, setEnabled] = React.useState(true);
  const [speaking, setSpeaking] = React.useState<Speaking>(null);

  const enabledRef = React.useRef(true);
  enabledRef.current = enabled;
  const voicesRef = React.useRef<SpeechSynthesisVoice[]>([]);
  const queueRef = React.useRef<Job[]>([]);
  const runningRef = React.useRef(false);
  const idleWaiters = React.useRef<Array<() => void>>([]);
  const generation = React.useRef(0);

  React.useEffect(() => {
    if (!supported) return;
    const load = () => {
      voicesRef.current = window.speechSynthesis.getVoices().filter((v) => v.lang.toLowerCase().startsWith("en"));
    };
    load();
    window.speechSynthesis.addEventListener("voiceschanged", load);
    return () => {
      window.speechSynthesis.removeEventListener("voiceschanged", load);
      window.speechSynthesis.cancel();
    };
  }, [supported]);

  const flushIdle = React.useCallback(() => {
    if (runningRef.current || queueRef.current.length) return;
    const waiters = idleWaiters.current.splice(0);
    waiters.forEach((resolve) => resolve());
  }, []);

  const voiceFor = React.useCallback((role: Role, name: string) => {
    const all = voicesRef.current;
    if (!all.length) return { voice: undefined, pitch: role === "delegate" ? 1.1 : 0.8, rate: 1 };
    const delegate = pickByName(all, DELEGATE_PREFERRED)[0] ?? all[0];
    if (role === "delegate") return { voice: delegate, pitch: 1.05, rate: 1.0 };
    const pool = pickByName(all, HUMAN_PREFERRED).filter((v) => v !== delegate);
    const rest = all.filter((v) => v !== delegate);
    const candidates = pool.length ? pool : rest.length ? rest : [delegate];
    const voice = candidates[hash(name) % candidates.length];
    // When only one voice exists, pitch alone must still tell the speakers apart.
    const sameVoice = voice === delegate;
    return { voice, pitch: sameVoice ? 0.7 + (hash(name) % 3) * 0.1 : 0.95, rate: 1.02 };
  }, []);

  const pump = React.useCallback(() => {
    if (runningRef.current) return;
    const job = queueRef.current.shift();
    if (!job) {
      setSpeaking(null);
      flushIdle();
      return;
    }
    runningRef.current = true;
    const mine = generation.current;
    let settled = false;
    let guard = 0;
    const finish = () => {
      if (settled) return;
      settled = true;
      window.clearTimeout(guard);
      if (generation.current !== mine) return;
      runningRef.current = false;
      job.done();
      pump();
    };
    const utterance = new SpeechSynthesisUtterance(job.text);
    const { voice, pitch, rate } = voiceFor(job.role, job.name);
    if (voice) {
      utterance.voice = voice;
      utterance.lang = voice.lang;
    }
    utterance.pitch = pitch;
    utterance.rate = rate;
    utterance.onend = finish;
    utterance.onerror = finish;
    setSpeaking({ role: job.role, name: job.name });
    // Safety net: some browsers never fire onend for a blocked utterance.
    guard = window.setTimeout(finish, Math.max(4000, job.text.length * 110));
    window.speechSynthesis.speak(utterance);
  }, [flushIdle, voiceFor]);

  /** Queue a line. Resolves when it has been spoken (or at once when voice is off). */
  const speak = React.useCallback(
    (role: Role, name: string, text: string): Promise<void> => {
      if (!supported || !enabledRef.current || !text.trim()) return Promise.resolve();
      return new Promise<void>((resolve) => {
        queueRef.current.push({ role, name, text, done: resolve });
        pump();
      });
    },
    [supported, pump],
  );

  /** Resolves once everything queued has been spoken. */
  const idle = React.useCallback((): Promise<void> => {
    if (!supported || (!runningRef.current && !queueRef.current.length)) return Promise.resolve();
    return new Promise<void>((resolve) => idleWaiters.current.push(resolve));
  }, [supported]);

  const cancel = React.useCallback(() => {
    generation.current += 1;
    const dropped = queueRef.current.splice(0);
    dropped.forEach((job) => job.done());
    runningRef.current = false;
    if (supported) window.speechSynthesis.cancel();
    setSpeaking(null);
    flushIdle();
  }, [supported, flushIdle]);

  const toggle = React.useCallback(() => {
    setEnabled((on) => {
      if (on) cancel();
      return !on;
    });
  }, [cancel]);

  return { supported, enabled, speaking, speak, idle, cancel, toggle };
}
