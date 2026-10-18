"use client";

import * as React from "react";
import { Lock, UserCog } from "lucide-react";
import { api, errorMessage } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/components/toast";

export function AccountCard() {
  const { account, reload } = useAuth();
  const toast = useToast();
  const [principalName, setPrincipalName] = React.useState(account?.principal?.display_name ?? "");
  const [aliases, setAliases] = React.useState((account?.principal?.aliases ?? []).join(", "));
  const [current, setCurrent] = React.useState("");
  const [next, setNext] = React.useState("");
  const [busy, setBusy] = React.useState<"profile" | "password" | null>(null);

  React.useEffect(() => {
    setPrincipalName(account?.principal?.display_name ?? "");
    setAliases((account?.principal?.aliases ?? []).join(", "));
  }, [account]);

  async function saveProfile(event: React.FormEvent) {
    event.preventDefault();
    setBusy("profile");
    try {
      await api.updateAccount({
        principal_name: principalName.trim(),
        aliases: aliases.split(",").map((a) => a.trim()).filter(Boolean),
      });
      await reload();
      toast.push({ title: "Saved", description: "Your delegate answers to that name from the next meeting." });
    } catch (err) {
      toast.push({ title: "Could not save", description: errorMessage(err), variant: "error" });
    } finally {
      setBusy(null);
    }
  }

  async function changePassword(event: React.FormEvent) {
    event.preventDefault();
    setBusy("password");
    try {
      const result = await api.changePassword(current, next);
      toast.push({ title: "Password changed", description: result.detail });
      setCurrent("");
      setNext("");
    } catch (err) {
      toast.push({ title: "Could not change it", description: errorMessage(err), variant: "error" });
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <UserCog className="size-4 text-muted-foreground" aria-hidden="true" /> Account
        </CardTitle>
        <CardDescription>
          {account?.email} · principal id <code className="font-mono">{account?.principal?.slug}</code>
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        <form onSubmit={saveProfile} className="space-y-3">
          <div className="space-y-1.5">
            <Label htmlFor="principal">Name used in meetings</Label>
            <Input id="principal" value={principalName} onChange={(e) => setPrincipalName(e.target.value)} required />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="aliases">Also addressed as</Label>
            <Input
              id="aliases"
              value={aliases}
              onChange={(e) => setAliases(e.target.value)}
              placeholder="Nicknames, comma separated"
            />
            <p className="text-[11px] text-muted-foreground">
              The delegate only answers when it hears one of these, so add what people actually call you.
            </p>
          </div>
          <Button type="submit" size="sm" loading={busy === "profile"}>
            Save
          </Button>
        </form>

        <form onSubmit={changePassword} className="space-y-3 border-t border-border pt-4">
          <p className="flex items-center gap-2 text-sm font-medium">
            <Lock className="size-3.5 text-muted-foreground" aria-hidden="true" /> Change password
          </p>
          <div className="space-y-1.5">
            <Label htmlFor="current">Current password</Label>
            <Input id="current" type="password" value={current} onChange={(e) => setCurrent(e.target.value)} required />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="next">New password</Label>
            <Input id="next" type="password" minLength={10} value={next} onChange={(e) => setNext(e.target.value)} required />
          </div>
          <p className="text-[11px] text-muted-foreground">Every other session is signed out, including this one.</p>
          <Button type="submit" size="sm" variant="outline" loading={busy === "password"}>
            Change password
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
