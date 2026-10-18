"use client";

import * as React from "react";
import { api, errorMessage } from "@/lib/api";
import { API_BASE } from "@/lib/config";
import { useAuth } from "@/lib/auth";
import { Button } from "@/components/ui/button";
import type { Account, OAuthLoginPopupMessage } from "@/lib/types";

interface SsoButtonsProps {
  /** "signup" passes accept_terms=true through so a brand-new SSO account can be created. */
  mode: "login" | "signup";
  disabled?: boolean;
  onAuthenticated: (account: Account) => void;
  onError: (message: string) => void;
}

/**
 * "Continue with Google" / "Continue with Microsoft" buttons.
 *
 * Opens a popup at the backend's oauth/start endpoint and waits for the
 * callback popup to postMessage a result back - the same mechanic already
 * proven for connectors in web/components/connectors/connectors-view.tsx
 * (connectOAuth + its onMessage effect), except the payload here carries a
 * full session (tokens + account) rather than a plain connected/ok flag,
 * since there is no prior authenticated request to attach anything to.
 */
export function SsoButtons({ mode, disabled, onAuthenticated, onError }: SsoButtonsProps) {
  const { completeOAuthLogin } = useAuth();
  const [busy, setBusy] = React.useState<"google" | "microsoft" | null>(null);

  React.useEffect(() => {
    function onMessage(event: MessageEvent) {
      if (event.origin !== API_BASE) return;
      const data = event.data as OAuthLoginPopupMessage | undefined;
      if (!data || data.source !== "deleg8-oauth-login") return;
      setBusy(null);
      if (data.ok && data.tokens) {
        onAuthenticated(completeOAuthLogin(data.tokens));
      } else {
        onError(data.message || "Could not sign in");
      }
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function start(provider: "google" | "microsoft") {
    onError("");
    setBusy(provider);
    try {
      const { redirect_url } = await api.startSsoLogin(provider, window.location.origin, mode === "signup");
      const popup = window.open(redirect_url, "deleg8-sso", "width=480,height=640,noopener=no");
      if (!popup) {
        onError("Allow popups for this site and try again.");
        setBusy(null);
      }
    } catch (err) {
      onError(errorMessage(err));
      setBusy(null);
    }
  }

  return (
    <div className="space-y-2.5">
      <Button
        type="button"
        variant="outline"
        size="lg"
        className="w-full justify-center border-border/80 bg-card/60 font-medium"
        disabled={disabled}
        loading={busy === "google"}
        onClick={() => start("google")}
      >
        Continue with Google
      </Button>
      <Button
        type="button"
        variant="outline"
        size="lg"
        className="w-full justify-center border-border/80 bg-card/60 font-medium"
        disabled={disabled}
        loading={busy === "microsoft"}
        onClick={() => start("microsoft")}
      >
        Continue with Microsoft
      </Button>
    </div>
  );
}
