"use client";

import * as React from "react";
import { Check, Copy, MessageSquare, Unlink } from "lucide-react";
import { api, errorMessage } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/components/toast";
import type { ChannelSummary, SettingsResponse } from "@/lib/types";

export function ChannelCard({ settings, onSaved }: { settings: SettingsResponse; onSaved: () => void }) {
  const toast = useToast();
  const [channel, setChannel] = React.useState<ChannelSummary>(settings.channel);
  const [busy, setBusy] = React.useState<string | null>(null);
  const [showOwnBot, setShowOwnBot] = React.useState(false);
  const [token, setToken] = React.useState("");
  const [chatId, setChatId] = React.useState("");
  const [copied, setCopied] = React.useState(false);

  React.useEffect(() => setChannel(settings.channel), [settings.channel]);

  async function run(name: string, fn: () => Promise<ChannelSummary>, message: string) {
    setBusy(name);
    try {
      setChannel(await fn());
      onSaved();
      toast.push({ title: message });
    } catch (err) {
      toast.push({ title: "That did not work", description: errorMessage(err), variant: "error" });
    } finally {
      setBusy(null);
    }
  }

  const command = `/link ${channel.link_code ?? ""}`;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <MessageSquare className="size-4" aria-hidden="true" /> Where approvals reach you
        </CardTitle>
        <CardDescription>{channel.describe}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {channel.linked ? (
          <div className="space-y-2">
            <p className="flex items-center gap-2 text-sm text-success">
              <Check className="size-4" aria-hidden="true" />
              Connected {channel.own_bot ? "through your own bot" : `to ${channel.bot_username ? `@${channel.bot_username}` : "the DELEG8 bot"}`}
              {channel.label ? ` (${channel.label})` : ""}
            </p>
            <Button
              size="sm"
              variant="outline"
              loading={busy === "unlink"}
              onClick={() => run("unlink", api.unlinkTelegram, "Disconnected")}
            >
              <Unlink className="size-3.5" /> Disconnect
            </Button>
          </div>
        ) : (
          <div className="space-y-3">
            {channel.link_code ? (
              <div className="flex items-center gap-2">
                <code className="flex-1 rounded-md border border-border bg-muted px-3 py-2 font-mono text-sm">{command}</code>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    void navigator.clipboard.writeText(command);
                    setCopied(true);
                    window.setTimeout(() => setCopied(false), 1500);
                  }}
                >
                  {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
                </Button>
              </div>
            ) : null}
            <p className="text-xs text-muted-foreground">
              Send that to {channel.bot_username ? `@${channel.bot_username}` : "the DELEG8 bot"} in Telegram.
            </p>
            <Button
              size="sm"
              loading={busy === "code"}
              onClick={() => run("code", api.settingsTelegramCode, "New code issued")}
            >
              {channel.link_code ? "New code" : "Get a code"}
            </Button>
          </div>
        )}

        <div className="border-t border-border pt-3">
          <button
            type="button"
            className="text-xs text-muted-foreground underline hover:text-foreground"
            onClick={() => setShowOwnBot((v) => !v)}
          >
            {showOwnBot ? "Hide" : "Use your own Telegram bot instead"}
          </button>
          {showOwnBot ? (
            <div className="mt-3 space-y-3">
              <p className="text-[11px] text-muted-foreground">
                Worth doing if you would rather this deployment&apos;s operator had no visibility of the bot your
                approvals pass through. You create it with @BotFather and run nothing yourself.
              </p>
              <div className="space-y-1.5">
                <Label htmlFor="bot-token">Bot token</Label>
                <Input id="bot-token" type="password" value={token} onChange={(e) => setToken(e.target.value)} />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="chat-id">Your chat id</Label>
                <Input id="chat-id" value={chatId} onChange={(e) => setChatId(e.target.value)} placeholder="From @userinfobot" />
              </div>
              <Button
                size="sm"
                loading={busy === "own-bot"}
                onClick={() => run("own-bot", () => api.useOwnBot(token.trim(), chatId.trim()), "Your bot is connected")}
              >
                Use my bot
              </Button>
            </div>
          ) : null}
        </div>
      </CardContent>
    </Card>
  );
}
