"use client";

import * as React from "react";
import { api, errorMessage } from "@/lib/api";
import { useApi } from "@/hooks/use-api";
import { PageHeader } from "@/components/page-header";
import { Spinner } from "@/components/ui/spinner";
import { AccountCard } from "@/components/settings/account-card";
import { ConsentCard } from "@/components/settings/consent-card";
import { CredentialsCard } from "@/components/settings/credentials-card";
import { ChannelCard } from "@/components/settings/channel-card";
import { DangerCard } from "@/components/settings/danger-card";

export function SettingsView() {
  const settings = useApi(api.settings);

  if (settings.loading && !settings.data) {
    return (
      <div className="grid min-h-[40vh] place-items-center">
        <Spinner />
      </div>
    );
  }
  if (settings.error) {
    return <p className="text-sm text-danger">{errorMessage(settings.error)}</p>;
  }
  const data = settings.data!;

  return (
    <div className="space-y-8">
      <PageHeader
        title="Settings"
        description="Who your delegate speaks for, how it reaches you, and how it handles consent."
      />
      <div className="grid gap-6 lg:grid-cols-2">
        <AccountCard />
        <ConsentCard settings={data} onSaved={settings.refresh} />
        <ChannelCard settings={data} onSaved={settings.refresh} />
        <CredentialsCard settings={data} onSaved={settings.refresh} />
      </div>
      <div className="space-y-3 border-t border-border pt-6">
        <p className="font-mono text-[10.5px] uppercase tracking-[0.12em] text-muted-foreground">Danger zone</p>
        <DangerCard />
      </div>
    </div>
  );
}
