/**
 * Token storage and silent refresh, outside React.
 *
 * `lib/api.ts` needs the access token on every request and needs to be able to
 * refresh it, but it is a plain module - it cannot read a React context. So the
 * tokens live here, in a tiny observable store that both the fetch layer and
 * the auth context read from.
 *
 * Refresh is deliberately single-flight: a page that mounts six widgets will
 * fire six requests, and if the token has just expired all six get a 401 at
 * once. Without the shared promise below, each would refresh independently -
 * and because the server rotates refresh tokens on use, five of those six would
 * be refreshing with a token that had already been consumed, signing the user
 * out of a perfectly good session.
 */

import { API_BASE } from "@/lib/config";

const ACCESS_KEY = "deleg8.access";
const REFRESH_KEY = "deleg8.refresh";

export interface Tokens {
  access: string;
  refresh: string;
}

type Listener = (tokens: Tokens | null) => void;

let tokens: Tokens | null = null;
let loaded = false;
const listeners = new Set<Listener>();
let refreshing: Promise<Tokens | null> | null = null;

function load(): Tokens | null {
  if (loaded) return tokens;
  loaded = true;
  if (typeof window === "undefined") return null;
  try {
    const access = window.localStorage.getItem(ACCESS_KEY);
    const refresh = window.localStorage.getItem(REFRESH_KEY);
    tokens = access && refresh ? { access, refresh } : null;
  } catch {
    tokens = null;
  }
  return tokens;
}

function persist(next: Tokens | null): void {
  if (typeof window === "undefined") return;
  try {
    if (next) {
      window.localStorage.setItem(ACCESS_KEY, next.access);
      window.localStorage.setItem(REFRESH_KEY, next.refresh);
    } else {
      window.localStorage.removeItem(ACCESS_KEY);
      window.localStorage.removeItem(REFRESH_KEY);
    }
  } catch {
    /* private browsing: the session simply does not survive a reload */
  }
}

export function getTokens(): Tokens | null {
  return load();
}

export function getAccessToken(): string | null {
  return load()?.access ?? null;
}

export function setTokens(next: Tokens | null): void {
  loaded = true;
  tokens = next;
  persist(next);
  for (const listener of listeners) listener(next);
}

export function clearTokens(): void {
  setTokens(null);
}

export function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Exchange the refresh token for a new pair. At most one call is ever in flight. */
export async function refreshTokens(): Promise<Tokens | null> {
  if (refreshing) return refreshing;
  const current = load();
  if (!current) return null;

  refreshing = (async () => {
    try {
      const response = await fetch(`${API_BASE}/api/v1/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: current.refresh }),
      });
      if (!response.ok) {
        clearTokens();
        return null;
      }
      const payload = (await response.json()) as { access_token: string; refresh_token: string };
      const next = { access: payload.access_token, refresh: payload.refresh_token };
      setTokens(next);
      return next;
    } catch {
      return null;
    } finally {
      refreshing = null;
    }
  })();
  return refreshing;
}
