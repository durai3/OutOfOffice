"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { AlertCircle, ShieldCheck } from "lucide-react";
import { AuthLayout } from "@/components/auth/auth-layout";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, errorMessage } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/utils";
import type { PolicyTemplate } from "@/lib/types";

export function SignupForm() {
  const { signup } = useAuth();
  const router = useRouter();
  const [name, setName] = React.useState("");
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [template, setTemplate] = React.useState("balanced");
  const [accepted, setAccepted] = React.useState(false);
  const [templates, setTemplates] = React.useState<PolicyTemplate[]>([]);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => {
    // Unauthenticated, so the picker can be shown before an account exists.
    void api
      .policyTemplates()
      .then(setTemplates)
      .catch(() => setTemplates([]));
  }, []);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await signup({
        email: email.trim(),
        password,
        display_name: name.trim(),
        accept_terms: accepted,
        policy_template: template,
      });
      router.replace("/onboarding");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthLayout
      title="Create your account"
      subtitle="Takes about a minute. You choose what your delegate is allowed to say."
      footer={
        <>
          Already have one? <Link href="/login" className="font-medium text-foreground underline">Sign in</Link>
        </>
      }
    >
      <form onSubmit={submit} className="space-y-4">
        <div className="space-y-1.5">
          <Label htmlFor="name">Your name</Label>
          <Input
            id="name"
            required
            maxLength={120}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="How you are addressed in meetings"
          />
          <p className="text-[11px] text-muted-foreground">This is the name your delegate answers to in a call.</p>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="email">Email</Label>
          <Input id="email" type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="password">Password</Label>
          <Input
            id="password"
            type="password"
            autoComplete="new-password"
            required
            minLength={10}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <p className="text-[11px] text-muted-foreground">At least 10 characters.</p>
        </div>

        {templates.length > 0 ? (
          <fieldset className="space-y-2">
            <legend className="text-sm font-medium">Starting policy</legend>
            <p className="text-[11px] text-muted-foreground">
              You can change every rule afterwards. All of them refuse pay, contracts and personal matters.
            </p>
            <div className="space-y-2">
              {templates.map((item) => (
                <label
                  key={item.key}
                  className={cn(
                    "flex cursor-pointer gap-3 rounded-md border p-3 transition-colors",
                    template === item.key ? "border-primary bg-accent" : "border-border hover:bg-accent/50",
                  )}
                >
                  <input
                    type="radio"
                    name="template"
                    value={item.key}
                    checked={template === item.key}
                    onChange={() => setTemplate(item.key)}
                    className="mt-1 accent-[var(--primary)]"
                  />
                  <span className="min-w-0">
                    <span className="flex items-center gap-2 text-sm font-medium">
                      {item.name}
                      <span className="text-[10px] font-normal text-muted-foreground">
                        {item.tiers.GREEN} green · {item.tiers.YELLOW} approval · {item.tiers.RED} never
                      </span>
                    </span>
                    <span className="mt-0.5 block text-xs text-muted-foreground">{item.summary}</span>
                  </span>
                </label>
              ))}
            </div>
          </fieldset>
        ) : null}

        <label className="flex cursor-pointer items-start gap-2.5 rounded-md border border-border p-3 text-xs">
          <input
            type="checkbox"
            checked={accepted}
            onChange={(e) => setAccepted(e.target.checked)}
            className="mt-0.5 accent-[var(--primary)]"
            required
          />
          <span className="text-muted-foreground">
            I accept the{" "}
            <Link href="/legal/terms" className="underline hover:text-foreground" target="_blank">Terms of Service</Link> and{" "}
            <Link href="/legal/privacy" className="underline hover:text-foreground" target="_blank">Privacy Policy</Link>, and I
            confirm I will only send a delegate to meetings I am entitled to bring one to.
          </span>
        </label>

        {error ? (
          <p className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/5 p-2.5 text-xs text-danger">
            <AlertCircle className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
            {error}
          </p>
        ) : null}

        <Button type="submit" className="w-full" loading={busy} disabled={!accepted}>
          <ShieldCheck className="size-4" /> Create account
        </Button>
      </form>
    </AuthLayout>
  );
}
