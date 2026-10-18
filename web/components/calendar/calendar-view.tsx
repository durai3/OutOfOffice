"use client";

import * as React from "react";
import { Bot, CalendarClock, Link2, Mail, RefreshCw, Send, Unlink, VideoOff } from "lucide-react";
import { api, errorMessage } from "@/lib/api";
import { API_BASE } from "@/lib/config";
import { useApi } from "@/hooks/use-api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/toast";
import { PageHeader } from "@/components/page-header";
import type { CalendarAccount, CalendarEvent, CalendarOAuthPopupMessage, CalendarProviderInfo } from "@/lib/types";

/** Display name for a provider id, purely cosmetic - the badge above it already carries the real `label` field. */
function providerName(provider: string): string {
  if (provider === "google") return "Google";
  if (provider === "microsoft") return "Microsoft";
  return provider;
}

function groupByDay(events: CalendarEvent[]): [string, CalendarEvent[]][] {
  const groups = new Map<string, CalendarEvent[]>();
  for (const event of events) {
    const key = new Date(event.start_at).toDateString();
    const bucket = groups.get(key);
    if (bucket) bucket.push(event);
    else groups.set(key, [event]);
  }
  return Array.from(groups.entries());
}

function dayLabel(dateString: string): string {
  const date = new Date(dateString);
  const today = new Date();
  const tomorrow = new Date(today);
  tomorrow.setDate(today.getDate() + 1);
  if (date.toDateString() === today.toDateString()) return "Today";
  if (date.toDateString() === tomorrow.toDateString()) return "Tomorrow";
  return date.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" });
}

function timeLabel(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

export function CalendarView() {
  const providers = useApi<CalendarProviderInfo[]>(api.calendarProviders);
  const accounts = useApi<CalendarAccount[]>(api.calendarAccounts);
  const events = useApi<CalendarEvent[]>(api.calendarEvents, { refreshMs: 30_000 });
  const toast = useToast();

  React.useEffect(() => {
    function onMessage(event: MessageEvent) {
      if (event.origin !== API_BASE) return;
      const data = event.data as CalendarOAuthPopupMessage | undefined;
      if (!data || data.source !== "deleg8-calendar-oauth") return;
      if (data.ok) {
        toast.push({ title: "Calendar connected", variant: "success" });
      } else {
        toast.push({ title: "Could not connect", description: data.message || "the provider declined", variant: "error" });
      }
      void accounts.refresh();
      void events.refresh();
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function connect(provider: string) {
    try {
      const { redirect_url } = await api.startCalendarOAuth(provider, window.location.origin);
      const popup = window.open(redirect_url, "deleg8-calendar-oauth", "width=540,height=720,noopener=no");
      if (!popup) {
        toast.push({ title: "Popup blocked", description: "Allow popups for this site and try connecting again.", variant: "error" });
      }
    } catch (err) {
      toast.push({ title: "Could not start connection", description: errorMessage(err), variant: "error" });
    }
  }

  return (
    <>
      <PageHeader
        title="Calendar"
        description="Connect your mailboxes and your delegate will see what's coming up. Flag a meeting ahead of time, or from the Telegram reminder, and it joins on its own."
      />
      <div className="space-y-6">
        <AccountsSection
          providers={providers.data}
          accounts={accounts.data}
          loading={!providers.data || !accounts.data}
          onConnect={connect}
          onChanged={() => {
            void accounts.refresh();
            void events.refresh();
          }}
        />
        <EventsSection events={events.data} loading={!events.data} onChanged={() => void events.refresh()} />
      </div>
    </>
  );
}

function AccountsSection({
  providers,
  accounts,
  loading,
  onConnect,
  onChanged,
}: {
  providers: CalendarProviderInfo[] | undefined;
  accounts: CalendarAccount[] | undefined;
  loading: boolean;
  onConnect: (provider: string) => void;
  onChanged: () => void;
}) {
  if (loading) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-16" />
      </div>
    );
  }

  const connectedByProvider = new Map<string, number>();
  for (const account of accounts ?? []) {
    connectedByProvider.set(account.provider, (connectedByProvider.get(account.provider) ?? 0) + 1);
  }

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between gap-3 space-y-0 pb-3">
        <div className="min-w-0">
          <CardTitle className="flex items-center gap-2">
            <Link2 className="size-4 text-muted-foreground" aria-hidden="true" /> Connected calendars
          </CardTitle>
          <CardDescription className="mt-1">
            Connect on top of however you signed in - a work Google account, a personal one, or Outlook, all at once.
          </CardDescription>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          {providers?.map((provider) => (
            <Button
              key={provider.provider}
              size="sm"
              variant={provider.available ? "default" : "outline"}
              disabled={!provider.available}
              title={provider.available ? undefined : provider.unavailable_reason ?? "not configured"}
              onClick={() => onConnect(provider.provider)}
            >
              <Link2 className="size-3.5" />
              Connect {provider.label}
              {!provider.available ? <span className="text-muted-foreground">(coming soon)</span> : null}
            </Button>
          ))}
        </div>
      </CardHeader>
      {accounts && accounts.length > 0 ? (
        <CardContent className="space-y-2 border-t border-border pt-3">
          {accounts.map((account) => (
            <AccountRow key={account.id} account={account} onChanged={onChanged} />
          ))}
        </CardContent>
      ) : null}
    </Card>
  );
}

function AccountRow({ account, onChanged }: { account: CalendarAccount; onChanged: () => void }) {
  const toast = useToast();
  const [busy, setBusy] = React.useState(false);

  async function toggle(enabled: boolean) {
    setBusy(true);
    try {
      await api.patchCalendarAccount(account.id, enabled);
      onChanged();
    } catch (err) {
      toast.push({ title: "Could not update", description: errorMessage(err), variant: "error" });
    } finally {
      setBusy(false);
    }
  }

  async function sync() {
    setBusy(true);
    try {
      await api.syncCalendarAccount(account.id);
      onChanged();
      toast.push({ title: `Synced ${account.email}` });
    } catch (err) {
      toast.push({ title: "Sync failed", description: errorMessage(err), variant: "error" });
    } finally {
      setBusy(false);
    }
  }

  async function disconnect() {
    setBusy(true);
    try {
      await api.disconnectCalendarAccount(account.id);
      toast.push({ title: `Disconnected ${account.email}` });
      onChanged();
    } catch (err) {
      toast.push({ title: "Could not disconnect", description: errorMessage(err), variant: "error" });
    } finally {
      setBusy(false);
    }
  }

  const statusVariant: "danger" | "success" | "secondary" = account.last_error ? "danger" : account.is_enabled ? "success" : "secondary";
  const statusLabel = account.last_error ? "Sync failed" : account.is_enabled ? "Active" : "Paused";

  return (
    <div className="flex items-center gap-3 rounded-lg border border-border px-3 py-2.5">
      <div className="flex size-9 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground">
        <Mail className="size-4" aria-hidden="true" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-sm font-medium">{account.email}</span>
          <Badge variant="outline">{account.label}</Badge>
          <Badge variant={statusVariant} dot>
            {statusLabel}
          </Badge>
        </div>
        <p className="mt-0.5 truncate text-xs text-muted-foreground">
          <span className="text-foreground/70">{providerName(account.provider)}</span>
          <span aria-hidden="true"> &middot; </span>
          {account.last_error
            ? `Sync failed: ${account.last_error}`
            : account.last_synced_at
              ? `Last synced ${new Date(account.last_synced_at).toLocaleString()}`
              : "Not synced yet"}
        </p>
      </div>
      <Switch checked={account.is_enabled} onCheckedChange={toggle} disabled={busy} aria-label={`Enable ${account.email}`} />
      <Button size="sm" variant="outline" loading={busy} onClick={sync} aria-label={`Sync ${account.email}`}>
        <RefreshCw className="size-3.5" />
      </Button>
      <Button size="sm" variant="outline" loading={busy} onClick={disconnect}>
        <Unlink className="size-3.5" /> Disconnect
      </Button>
    </div>
  );
}

function EventsSection({
  events,
  loading,
  onChanged,
}: {
  events: CalendarEvent[] | undefined;
  loading: boolean;
  onChanged: () => void;
}) {
  if (loading) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-14" />
        <Skeleton className="h-14" />
        <Skeleton className="h-14" />
      </div>
    );
  }

  if (!events || events.length === 0) {
    return (
      <Card>
        <CardContent>
          <EmptyState
            icon={<CalendarClock />}
            title="Nothing coming up"
            description="Connect a calendar above and your next seven days of meetings will show up here."
          />
        </CardContent>
      </Card>
    );
  }

  const groups = groupByDay(events);

  return (
    <div className="space-y-5">
      {groups.map(([day, dayEvents]) => (
        <div key={day}>
          <h2 className="mb-2 px-1 font-mono text-[10.5px] uppercase tracking-[0.12em] text-muted-foreground">
            {dayLabel(dayEvents[0].start_at)}
          </h2>
          <Card>
            <CardContent className="divide-y divide-border p-0">
              {dayEvents.map((event) => (
                <EventRow key={event.id} event={event} onChanged={onChanged} />
              ))}
            </CardContent>
          </Card>
        </div>
      ))}
    </div>
  );
}

function EventRow({ event, onChanged }: { event: CalendarEvent; onChanged: () => void }) {
  const toast = useToast();
  const [busy, setBusy] = React.useState(false);

  async function toggle(requested: boolean) {
    setBusy(true);
    try {
      await api.setEventDeleg8(event.id, requested);
      onChanged();
      if (requested) toast.push({ title: `Deleg8'd "${event.title}"`, variant: "success" });
    } catch (err) {
      toast.push({ title: "Could not update", description: errorMessage(err), variant: "error" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex items-center gap-3 px-4 py-3">
      <div className="tabular w-16 shrink-0 text-xs font-medium text-muted-foreground">{timeLabel(event.start_at)}</div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-sm font-medium">{event.title}</span>
          {event.dispatched ? (
            <Badge variant="success" dot>
              <Bot className="size-3" /> Dispatched
            </Badge>
          ) : event.deleg8_requested ? (
            <Badge variant="info">
              <Send className="size-3" /> Deleg8&apos;d
            </Badge>
          ) : null}
        </div>
        <p className="mt-0.5 truncate text-xs text-muted-foreground">{event.account_email}</p>
      </div>
      {event.delegable ? (
        <div className="flex items-center gap-2">
          <span className="text-xs text-muted-foreground">Deleg8 it</span>
          <Switch
            checked={event.deleg8_requested}
            onCheckedChange={toggle}
            disabled={busy || event.dispatched}
            aria-label={`Deleg8 ${event.title}`}
          />
        </div>
      ) : (
        <span className="flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground" title="No video link on this event">
          <VideoOff className="size-3.5" /> No video link
        </span>
      )}
    </div>
  );
}
