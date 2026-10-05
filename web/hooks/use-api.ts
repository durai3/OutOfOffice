"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage } from "@/lib/api";

interface Options {
  /** Poll interval in milliseconds; omit for a one-shot fetch. */
  refreshMs?: number;
  /** When false the hook does nothing (useful while a dependency is missing). */
  enabled?: boolean;
  /** Change this string to force a refetch when the fetcher's inputs change. */
  key?: string;
}

interface State<T> {
  data: T | undefined;
  error: string | null;
  loading: boolean;
  updatedAt: number | null;
}

export function useApi<T>(fetcher: () => Promise<T>, { refreshMs, enabled = true, key = "" }: Options = {}) {
  const [state, setState] = useState<State<T>>({ data: undefined, error: null, loading: true, updatedAt: null });
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const inFlight = useRef(false);

  const refresh = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      const data = await fetcherRef.current();
      setState({ data, error: null, loading: false, updatedAt: Date.now() });
    } catch (error) {
      setState((prev) => ({ ...prev, error: errorMessage(error), loading: false }));
    } finally {
      inFlight.current = false;
    }
  }, []);

  useEffect(() => {
    if (!enabled) return;
    setState((prev) => ({ ...prev, loading: prev.data === undefined }));
    void refresh();
    if (!refreshMs) return;
    const tick = () => {
      if (document.visibilityState === "visible") void refresh();
    };
    const id = window.setInterval(tick, refreshMs);
    document.addEventListener("visibilitychange", tick);
    return () => {
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [refresh, refreshMs, enabled, key]);

  return { ...state, refresh };
}
