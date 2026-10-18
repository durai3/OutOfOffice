"use client";

import * as React from "react";
import {
  Calendar,
  Check,
  ChevronDown,
  ExternalLink,
  FileText,
  ListTodo,
  Mail,
  MessageSquare,
  Plug,
  Unlink,
  type LucideIcon,
} from "lucide-react";
import { api, errorMessage } from "@/lib/api";
import { API_BASE } from "@/lib/config";
import { useApi } from "@/hooks/use-api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/toast";
import { PageHeader } from "@/components/page-header";
import { cn } from "@/lib/utils";
import type { ConnectorOption, ConnectorSummary, OAuthPopupMessage } from "@/lib/types";

/** Purely cosmetic icon per connector type - no behavior depends on this mapping. */
const CONNECTOR_ICONS: Record<string, LucideIcon> = {
  slack: MessageSquare,
  ms_teams: MessageSquare,
  jira: ListTodo,
  linear: ListTodo,
  asana: ListTodo,
  google_calendar: Calendar,
  email: Mail,
  notion: FileText,
};

function connectorIcon(connectorType: string): LucideIcon {
  return CONNECTOR_ICONS[connectorType] ?? Plug;
}

export function ConnectorsView() {
  const connectors = useApi<ConnectorSummary[]>(api.integrations);
  const toast = useToast();

  React.useEffect(() => {
    function onMessage(event: MessageEvent) {
      if (event.origin !== API_BASE) return;
      const data = event.data as OAuthPopupMessage | undefined;
      if (!data || data.source !== "deleg8-oauth") return;
      if (data.ok) {
        toast.push({ title: `Connected ${data.connector_type}`, variant: "success" });
      } else {
        toast.push({ title: "Could not connect", description: data.message || "the provider declined", variant: "error" });
      }
      void connectors.refresh();
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const data = connectors.data;
  // A connector the operator hasn't registered an OAuth app for yet (and that
  // nobody has somehow already connected) reads as "broken" if it's shown
  // alongside the ones that actually work. Keep those out of the default
  // view - still reachable, just not the first thing a new account sees.
  const ready = data?.filter((connector) => connector.available || connector.is_connected) ?? [];
  const comingSoon = data?.filter((connector) => !connector.available && !connector.is_connected) ?? [];

  return (
    <>
      <PageHeader
        title="Connectors"
        description="Where post-meeting briefings go, and which tools your action items sync to. Connect, configure, and toggle each one yourself."
      />
      <div className="space-y-3">
        {!data ? (
          <>
            <Skeleton className="h-20" />
            <Skeleton className="h-20" />
            <Skeleton className="h-20" />
          </>
        ) : (
          <>
            {ready.map((connector) => (
              <ConnectorCard key={connector.connector_type} connector={connector} onChanged={connectors.refresh} />
            ))}
            {comingSoon.length > 0 ? (
              <details className="group rounded-lg border border-border bg-card">
                <summary className="flex cursor-pointer list-none items-center justify-between gap-2 px-4 py-3 text-sm text-muted-foreground transition-colors hover:text-foreground">
                  <span className="flex items-center gap-2">
                    More integrations
                    <Badge variant="secondary">{comingSoon.length}</Badge>
                    <span className="text-muted-foreground/70">coming soon</span>
                  </span>
                  <ChevronDown className="size-3.5 shrink-0 transition-transform group-open:rotate-180" />
                </summary>
                <div className="space-y-3 border-t border-border p-3">
                  {comingSoon.map((connector) => (
                    <ConnectorCard key={connector.connector_type} connector={connector} onChanged={connectors.refresh} />
                  ))}
                </div>
              </details>
            ) : null}
          </>
        )}
      </div>
    </>
  );
}

function ConnectorCard({ connector, onChanged }: { connector: ConnectorSummary; onChanged: () => void }) {
  const toast = useToast();
  const [open, setOpen] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [values, setValues] = React.useState<Record<string, string>>({});
  const isOAuth = connector.auth_mode === "oauth";

  async function toggle(enabled: boolean) {
    setBusy(true);
    try {
      await api.patchIntegration(connector.connector_type, { is_enabled: enabled });
      onChanged();
    } catch (err) {
      toast.push({ title: "Could not update", description: errorMessage(err), variant: "error" });
    } finally {
      setBusy(false);
    }
  }

  async function disconnect() {
    setBusy(true);
    try {
      await api.disconnectIntegration(connector.connector_type);
      toast.push({ title: `Disconnected ${connector.label}` });
      onChanged();
    } catch (err) {
      toast.push({ title: "Could not disconnect", description: errorMessage(err), variant: "error" });
    } finally {
      setBusy(false);
    }
  }

  async function connectToken() {
    const primary = connector.fields.find((f) => f.secret);
    const credential = primary ? values[primary.name]?.trim() || null : null;
    const config = Object.fromEntries(
      connector.fields.filter((f) => f.name !== primary?.name).map((f) => [f.name, values[f.name]?.trim() ?? ""]),
    );
    setBusy(true);
    try {
      await api.authorizeIntegration(connector.connector_type, credential, config);
      toast.push({ title: `Connected ${connector.label}`, variant: "success" });
      setOpen(false);
      setValues({});
      onChanged();
    } catch (err) {
      toast.push({ title: "Could not connect", description: errorMessage(err), variant: "error" });
    } finally {
      setBusy(false);
    }
  }

  async function connectOAuth() {
    setBusy(true);
    try {
      const { redirect_url } = await api.startOAuth(connector.connector_type, window.location.origin);
      const popup = window.open(redirect_url, "deleg8-oauth", "width=540,height=720,noopener=no");
      if (!popup) {
        toast.push({ title: "Popup blocked", description: "Allow popups for this site and try connecting again.", variant: "error" });
      }
    } catch (err) {
      toast.push({ title: "Could not start connection", description: errorMessage(err), variant: "error" });
    } finally {
      setBusy(false);
    }
  }

  const Icon = connectorIcon(connector.connector_type);

  return (
    <Card>
      <CardHeader className="flex-row items-center gap-3 space-y-0 pb-3">
        <div className="flex size-9 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground">
          <Icon className="size-4" aria-hidden="true" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium">{connector.label}</span>
            {connector.is_connected ? (
              <Badge variant="success" dot>
                <Check className="size-3" /> Connected
              </Badge>
            ) : !connector.available ? (
              <Badge variant="outline">Needs setup</Badge>
            ) : (
              <Badge variant="secondary">Not connected</Badge>
            )}
          </div>
          <p className="mt-0.5 truncate text-xs text-muted-foreground">
            {connector.is_connected
              ? connector.last_error
                ? `Last attempt failed: ${connector.last_error}`
                : connector.last_synced_at
                  ? `Last sent ${new Date(connector.last_synced_at).toLocaleString()}`
                  : "Connected; waiting for the next meeting to conclude"
              : connector.available
                ? isOAuth
                  ? `Connect your ${connector.label} account to get started`
                  : "Paste a credential to connect"
                : connector.unavailable_reason}
          </p>
        </div>
        {connector.is_connected ? (
          <>
            <Switch checked={connector.is_enabled} onCheckedChange={toggle} disabled={busy} aria-label={`Enable ${connector.label}`} />
            <Button size="sm" variant="outline" loading={busy} onClick={disconnect}>
              <Unlink className="size-3.5" /> Disconnect
            </Button>
          </>
        ) : connector.available && isOAuth ? (
          <Button size="sm" loading={busy} onClick={connectOAuth}>
            <Plug className="size-3.5" /> Connect with {connector.label}
          </Button>
        ) : connector.available ? (
          <Button size="sm" variant="outline" onClick={() => setOpen((v) => !v)}>
            Connect <ChevronDown className={cn("size-3.5 transition-transform", open && "rotate-180")} />
          </Button>
        ) : connector.docs_url ? (
          <a
            href={connector.docs_url}
            target="_blank"
            rel="noreferrer"
            className="shrink-0 text-xs text-muted-foreground underline hover:text-foreground"
          >
            Set it up <ExternalLink className="inline size-3" />
          </a>
        ) : null}
      </CardHeader>

      {open && !connector.is_connected && !isOAuth ? (
        <CardContent className="space-y-3 border-t border-border pt-3">
          <div className="grid gap-3 sm:grid-cols-2">
            {connector.fields.map((field) => (
              <div key={field.name} className="space-y-1.5">
                <Label htmlFor={`${connector.connector_type}-${field.name}`}>{field.label}</Label>
                <Input
                  id={`${connector.connector_type}-${field.name}`}
                  type={field.secret ? "password" : "text"}
                  placeholder={field.placeholder}
                  value={values[field.name] ?? ""}
                  onChange={(e) => setValues((prev) => ({ ...prev, [field.name]: e.target.value }))}
                />
              </div>
            ))}
          </div>
          <div className="flex items-center gap-3">
            <Button size="sm" loading={busy} onClick={connectToken}>
              Connect {connector.label}
            </Button>
            {connector.docs_url ? (
              <a href={connector.docs_url} target="_blank" rel="noreferrer" className="text-xs text-muted-foreground underline hover:text-foreground">
                Where do I get this?
              </a>
            ) : null}
          </div>
        </CardContent>
      ) : null}

      {connector.is_connected && isOAuth ? <FollowUpConfig connector={connector} onChanged={onChanged} /> : null}
    </Card>
  );
}

/** After an OAuth connector is connected, lets the user pick which project/board/calendar it should use - if it has one. */
function FollowUpConfig({ connector, onChanged }: { connector: ConnectorSummary; onChanged: () => void }) {
  const options = useApi<ConnectorOption[]>(() => api.connectorOptions(connector.connector_type), { key: connector.connector_type });
  const toast = useToast();
  const [busy, setBusy] = React.useState(false);
  const selected = (connector.config.target_id as string | undefined) ?? "";

  async function save(id: string) {
    setBusy(true);
    try {
      await api.patchIntegration(connector.connector_type, { config: { target_id: id } });
      onChanged();
    } catch (err) {
      toast.push({ title: "Could not save", description: errorMessage(err), variant: "error" });
    } finally {
      setBusy(false);
    }
  }

  if (options.loading || !options.data || options.data.length === 0) return null;

  return (
    <CardContent className="border-t border-border pt-3">
      <Label htmlFor={`${connector.connector_type}-target`}>Send to</Label>
      <Select
        id={`${connector.connector_type}-target`}
        className="mt-1.5 max-w-sm"
        value={selected}
        disabled={busy}
        onChange={(e) => void save(e.target.value)}
      >
        <option value="" disabled>
          Choose one&hellip;
        </option>
        {options.data.map((option) => (
          <option key={option.id} value={option.id}>
            {option.name}
          </option>
        ))}
      </Select>
    </CardContent>
  );
}
