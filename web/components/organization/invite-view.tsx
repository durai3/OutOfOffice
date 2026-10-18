"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { AlertCircle, Users } from "lucide-react";
import { AuthLayout } from "@/components/auth/auth-layout";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { api, errorMessage } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { InvitePreview } from "@/lib/types";

export function InviteView({ token }: { token: string }) {
  const { status, account, reload } = useAuth();
  const router = useRouter();
  const [preview, setPreview] = React.useState<InvitePreview | "loading" | "error">("loading");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    void api
      .previewInvite(token)
      .then((result) => setPreview(result.valid ? result : "error"))
      .catch(() => setPreview("error"));
  }, [token]);

  async function accept() {
    setBusy(true);
    setError(null);
    try {
      await api.acceptInvite(token);
      await reload();
      router.replace("/organization");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  if (preview === "loading" || status === "loading") {
    return (
      <div className="grid min-h-screen place-items-center">
        <Spinner />
      </div>
    );
  }

  if (preview === "error") {
    return (
      <AuthLayout title="Invite not found" subtitle="This link is invalid, expired, or has already been used.">
        <Button className="w-full" onClick={() => router.push("/")}>
          Go to Out of Office
        </Button>
      </AuthLayout>
    );
  }

  if (status === "authenticated" && account) {
    const emailMatches = account.email.toLowerCase() === preview.email.toLowerCase();
    return (
      <AuthLayout
        title="Join organization"
        subtitle={`You're invited to join ${preview.org_name} as ${preview.role}.`}
      >
        <div className="space-y-4">
          {!emailMatches ? (
            <p className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 p-2.5 text-xs text-foreground">
              <AlertCircle className="mt-0.5 size-3.5 shrink-0 text-warning-foreground" aria-hidden="true" />
              This invite was sent to {preview.email}, but you&apos;re signed in as {account.email}. Accepting may be
              refused - sign out and sign up with the invited address instead if it is.
            </p>
          ) : null}
          {error ? (
            <p className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/5 p-2.5 text-xs text-danger">
              <AlertCircle className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
              {error}
            </p>
          ) : null}
          <Button className="w-full" loading={busy} onClick={accept}>
            <Users className="size-4" /> Accept and join
          </Button>
        </div>
      </AuthLayout>
    );
  }

  return (
    <AuthLayout
      title="Join organization"
      subtitle={`You're invited to join ${preview.org_name} as ${preview.role}.`}
      footer={
        <>
          Already have an account?{" "}
          <Button
            variant="link"
            className="h-auto p-0 text-sm"
            onClick={() => router.push(`/login?next=${encodeURIComponent(`/invite/${token}`)}`)}
          >
            Log in
          </Button>
        </>
      }
    >
      <Button className="w-full" onClick={() => router.push(`/signup?invite=${encodeURIComponent(token)}`)}>
        <Users className="size-4" /> Create your account
      </Button>
    </AuthLayout>
  );
}
