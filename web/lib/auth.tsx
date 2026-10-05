"use client";

/**
 * The signed-in account, as React sees it.
 *
 * Holds the account, exposes signup/login/logout, and - importantly - tells
 * the rest of the app which of three states it is in: still checking, signed
 * out, or signed in. Rendering the console while "still checking" is what
 * produces the flash of an empty dashboard followed by a redirect, so
 * `status` is explicit rather than inferred from `account === null`.
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import { api, errorMessage } from "@/lib/api";
import { clearTokens, getTokens, setTokens, subscribe } from "@/lib/auth-store";
import type { Account, SignupRequest } from "@/lib/types";

export type AuthStatus = "loading" | "authenticated" | "anonymous";

interface AuthValue {
  status: AuthStatus;
  account: Account | null;
  error: string | null;
  login: (email: string, password: string) => Promise<Account>;
  signup: (body: SignupRequest) => Promise<Account>;
  logout: () => Promise<void>;
  reload: () => Promise<Account | null>;
  setAccount: (account: Account) => void;
}

const AuthContext = React.createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = React.useState<AuthStatus>("loading");
  const [account, setAccount] = React.useState<Account | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const router = useRouter();

  const reload = React.useCallback(async (): Promise<Account | null> => {
    if (!getTokens()) {
      setAccount(null);
      setStatus("anonymous");
      return null;
    }
    try {
      const me = await api.me();
      setAccount(me);
      setStatus("authenticated");
      return me;
    } catch (err) {
      clearTokens();
      setAccount(null);
      setStatus("anonymous");
      setError(errorMessage(err));
      return null;
    }
  }, []);

  React.useEffect(() => {
    void reload();
    // A sign-out in another tab clears the shared store; follow it here.
    return subscribe((tokens) => {
      if (!tokens) {
        setAccount(null);
        setStatus("anonymous");
      }
    });
  }, [reload]);

  const login = React.useCallback(async (email: string, password: string) => {
    setError(null);
    const payload = await api.login(email, password);
    setTokens({ access: payload.access_token, refresh: payload.refresh_token });
    setAccount(payload.account);
    setStatus("authenticated");
    return payload.account;
  }, []);

  const signup = React.useCallback(async (body: SignupRequest) => {
    setError(null);
    const payload = await api.signup(body);
    setTokens({ access: payload.access_token, refresh: payload.refresh_token });
    setAccount(payload.account);
    setStatus("authenticated");
    return payload.account;
  }, []);

  const logout = React.useCallback(async () => {
    const tokens = getTokens();
    if (tokens) {
      // Best effort: a failed revoke must not trap someone in a session they
      // are trying to leave, so the local tokens go either way.
      try {
        await api.logout(tokens.refresh);
      } catch {
        /* ignored */
      }
    }
    clearTokens();
    setAccount(null);
    setStatus("anonymous");
    router.push("/login");
  }, [router]);

  const value = React.useMemo<AuthValue>(
    () => ({ status, account, error, login, signup, logout, reload, setAccount }),
    [status, account, error, login, signup, logout, reload],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = React.useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside <AuthProvider>");
  return value;
}
