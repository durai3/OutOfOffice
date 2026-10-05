"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { AlertCircle } from "lucide-react";
import { AuthLayout } from "@/components/auth/auth-layout";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { errorMessage } from "@/lib/api";
import { useAuth } from "@/lib/auth";

export function LoginForm() {
  const { login } = useAuth();
  const router = useRouter();
  const params = useSearchParams();
  const next = params.get("next") || "/";
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const account = await login(email.trim(), password);
      router.replace(account.onboarding_complete ? next : "/onboarding");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthLayout
      title="Sign in"
      subtitle="Your delegate, your policy, your meetings."
      footer={
        <>
          No account yet? <Link href="/signup" className="font-medium text-foreground underline">Create one</Link>
        </>
      }
    >
      <form onSubmit={submit} className="space-y-4">
        <div className="space-y-1.5">
          <Label htmlFor="email">Email</Label>
          <Input
            id="email"
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="you@company.com"
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="password">Password</Label>
          <Input
            id="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>
        {error ? (
          <p className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/5 p-2.5 text-xs text-danger">
            <AlertCircle className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
            {error}
          </p>
        ) : null}
        <Button type="submit" className="w-full" loading={busy}>
          Sign in
        </Button>
      </form>
    </AuthLayout>
  );
}
