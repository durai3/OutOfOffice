"use client";

import * as React from "react";
import { useAuth } from "@/lib/auth";

/**
 * "Technical details": model names, timings, scores and index internals.
 *
 * Ordinary users never need them, so they are off by default. Evaluators (demo
 * accounts) get them on by default, because how it is built is part of what they
 * are judging. Anyone can flip it from the Playground; the choice is remembered
 * in this browser.
 */
const KEY = "ooo:technical-details";
const EVENT = "ooo:technical-details-changed";

function read(): "on" | "off" | null {
  try {
    const value = window.localStorage.getItem(KEY);
    return value === "on" || value === "off" ? value : null;
  } catch {
    return null;
  }
}

function subscribe(callback: () => void): () => void {
  window.addEventListener(EVENT, callback);
  window.addEventListener("storage", callback);
  return () => {
    window.removeEventListener(EVENT, callback);
    window.removeEventListener("storage", callback);
  };
}

export function useTechDetails(): [boolean, (on: boolean) => void] {
  const { account } = useAuth();
  const stored = React.useSyncExternalStore(subscribe, read, () => null);
  const on = stored ? stored === "on" : account?.settings?.demo === true;
  const set = React.useCallback((next: boolean) => {
    try {
      window.localStorage.setItem(KEY, next ? "on" : "off");
    } catch {
      /* private mode: the toggle just will not persist */
    }
    window.dispatchEvent(new Event(EVENT));
  }, []);
  return [on, set];
}
