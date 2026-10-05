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
        <CardTitle className="flex items-center gap-2 text-danger">
          <AlertTriangle className="size-4" aria-hidden="true" /> Delete this account
        </CardTitle>
        <CardDescription>
          Removes your policy, dossier, documents, vector index, meeting briefs, commitments, consent records and
          stored keys. It cannot be undone.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <Input
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          placeholder={`Type ${account?.email ?? "your email"} to confirm`}
          className="sm:max-w-sm"
        />
        <Button variant="destructive" disabled={!armed} loading={busy} onClick={remove}>
          Delete everything
        </Button>
      </CardContent>
    </Card>
  );
}
