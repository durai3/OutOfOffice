"use client";

/**
 * Decides what a given URL is allowed to render.
 *
 * Three rules, in order:
 *
 * 1. Signed out and asking for the console -> the sign-in page.
 * 2. Signed in and asking for the sign-in page -> the console.
 * 3. Signed in, but a required step is outstanding -> onboarding. This is the
 *    one that matters: a legal document whose version changed leaves the
 *    account able to sign in but not to dispatch, and the API enforces that
 *    with a 451. Sending them to the wizard is better than letting them find
 *    out when a meeting refuses to start.
 */

import * as React from "react";
import { usePathname, useRouter } from "next/navigation";
import { Spinner } from "@/components/ui/spinner";
import { JUDGE_LANDING } from "@/components/auth/judge-entry";
import { useAuth } from "@/lib/auth";

/** Reachable without an account. */
const ANONYMOUS_ROUTES = ["/login", "/signup", "/legal"];

/**
 * Reachable in *either* state, and never redirected away from.
 *
 * The legal documents belong here and the sign-in pages do not. A signed-in
 * user being bounced off `/legal/privacy` back to the dashboard means they
 * cannot read the policy they agreed to - which is exactly the document they
 * are most entitled to re-read at any time. An invite link is here for the
 * same reason, but from the other direction: whoever opens it might already
 * have an account (they should accept in place) or might not (they should
 * sign up) - either is a valid thing to do on the same URL, so neither state
 * gets redirected away from it.
 */
const ALWAYS_ROUTES = ["/legal", "/invite", "/judges"];

function matches(routes: string[], pathname: string): boolean {
  return routes.some((route) => pathname === route || pathname.startsWith(`${route}/`));
}

function isPublic(pathname: string): boolean {
  return matches(ANONYMOUS_ROUTES, pathname);
}

function isAlwaysAllowed(pathname: string): boolean {
  return matches(ALWAYS_ROUTES, pathname);
}

export function AuthGate({ children }: { children: React.ReactNode }) {
  const { status, account } = useAuth();
  const pathname = usePathname();
  const router = useRouter();
  const publicRoute = isPublic(pathname);
  const always = isAlwaysAllowed(pathname);
  const onboarding = pathname === "/onboarding";

  React.useEffect(() => {
    if (status === "loading" || always) return;
    if (status === "anonymous" && !publicRoute) {
      router.replace(`/login?next=${encodeURIComponent(pathname)}`);
      return;
    }
    if (status === "authenticated" && publicRoute) {
      // A one-click demo account has nothing to see on the dashboard yet; its home is the playground.
      router.replace(account?.settings?.demo === true ? JUDGE_LANDING : "/");
      return;
    }
    if (status === "authenticated" && account && !account.onboarding_complete && !onboarding) {
      router.replace("/onboarding");
    }
  }, [status, account, pathname, publicRoute, always, onboarding, router]);

  if (status === "loading") {
    return (
      <div className="grid min-h-screen place-items-center">
        <div className="flex items-center gap-3 text-sm text-muted-foreground">
          <Spinner /> Checking your session…
        </div>
      </div>
    );
  }
  if (always) return <>{children}</>;
  if (status === "anonymous" && !publicRoute) return null;
  if (status === "authenticated" && publicRoute) return null;
  return <>{children}</>;
}

export { isPublic };
