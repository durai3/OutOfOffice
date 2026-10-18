"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { AlertTriangle } from "lucide-react";
import { api, errorMessage } from "@/lib/api";
import { clearTokens } from "@/lib/auth-store";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/toast";
import { useAuth } from "@/lib/auth";

export function DangerCard() {
  const { account } = useAuth();
  const router = useRouter();
  const toast = useToast();
  const [confirm, setConfirm] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const armed = confirm.trim().toLowerCase() === (account?.email ?? "").toLowerCase();

  async function remove() {
    setBusy(true);
    try {
      await api.deleteAccount();
      clearTokens();
      router.replace("/signup");
    } catch (err) {
      toast.push({ title: "Could not delete the account", description: errorMessage(err), variant: "error" });
      setBusy(false);
    }
  }

  return (
    <Card className="border-danger/40">
      <CardHeader>
        <div className="flex items-start gap-3">
          <div className="grid size-10 shrink-0 place-items-center rounded-full bg-danger text-white">
            <AlertTriangle className="size-5" aria-hidden="true" />
          </div>
          <div className="min-w-0">
            <CardTitle className="text-danger">Delete this account</CardTitle>
            <CardDescription>
              Removes your rules, profile, documents, meeting briefs, action items, consent records and
              saved keys. It cannot be undone.
            </CardDescription>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        <div className="flex flex-col gap-2.5 rounded-lg border border-danger/30 bg-danger/5 p-3.5 sm:flex-row sm:items-center">
          <Input
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            placeholder={`Type ${account?.email ?? "your email"} to confirm`}
            className="sm:max-w-sm"
          />
          <Button variant="destructive" disabled={!armed} loading={busy} onClick={remove}>
            Delete everything
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
