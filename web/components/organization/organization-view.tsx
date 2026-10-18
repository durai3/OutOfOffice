"use client";

import * as React from "react";
import { Check, Copy, CreditCard, LogOut, Mail, ShieldCheck, Trash2, UserPlus, Users } from "lucide-react";
import { api, errorMessage } from "@/lib/api";
import { useApi } from "@/hooks/use-api";
import { useAuth } from "@/lib/auth";
import { Badge, type BadgeVariant } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { PageHeader } from "@/components/page-header";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Stat } from "@/components/ui/stat";
import { useToast } from "@/components/toast";
import { cn } from "@/lib/utils";
import type { OrganizationInvite, OrganizationMember, OrganizationRole } from "@/lib/types";

const ROLE_BADGE: Record<OrganizationRole, BadgeVariant> = { owner: "violet", admin: "info", member: "secondary" };

export function OrganizationView() {
  const { account, reload } = useAuth();
  const org = account?.organization ?? null;

  if (!org) return <NoOrganization onCreated={reload} />;
  return <OrganizationDashboard orgRole={org.role} />;
}

function NoOrganization({ onCreated }: { onCreated: () => Promise<unknown> }) {
  const toast = useToast();
  const [name, setName] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  async function create() {
    if (!name.trim()) return;
    setBusy(true);
    try {
      await api.createOrganization(name.trim());
      await onCreated();
      toast.push({ title: "Organization created", variant: "success" });
    } catch (err) {
      toast.push({ title: "Could not create organization", description: errorMessage(err), variant: "error" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader
        title="Team"
        description="Bring teammates onto your account: shared billing and seats, one admin view, everyone keeps their own meetings and knowledge private."
      />
      <Card className="border-dashed">
        <CardContent className="pt-6">
          <EmptyState
            icon={<Users />}
            title="You're not part of an organization yet"
            description="Create one to invite teammates. Your own meetings, knowledge and dossier stay exactly as private as they are today - an organization only adds shared billing and an admin view."
            action={
              <div className="flex flex-wrap items-center justify-center gap-2">
                <Input
                  placeholder="Organization name"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && void create()}
                  className="w-56"
                />
                <Button loading={busy} disabled={!name.trim()} onClick={create}>
                  Create organization
                </Button>
              </div>
            }
          />
        </CardContent>
      </Card>
    </>
  );
}

function OrganizationDashboard({ orgRole }: { orgRole: OrganizationRole }) {
  const { account, reload } = useAuth();
  const org = account!.organization!;
  const isAdmin = orgRole === "owner" || orgRole === "admin";
  const members = useApi<OrganizationMember[]>(api.organizationMembers);
  const invites = useApi<OrganizationInvite[]>(api.listInvites, { enabled: isAdmin });
  const toast = useToast();
  const [leaving, setLeaving] = React.useState(false);
  const [mintedInvite, setMintedInvite] = React.useState<OrganizationInvite | null>(null);

  async function leave() {
    setLeaving(true);
    try {
      await api.leaveOrganization();
      await reload();
      toast.push({ title: "You left the organization" });
    } catch (err) {
      toast.push({ title: "Could not leave", description: errorMessage(err), variant: "error" });
    } finally {
      setLeaving(false);
    }
  }

  return (
    <>
      <PageHeader
        title={org.name}
        description="Teammates on your account. Admins can invite and manage seats; everyone's own meetings and knowledge stay private."
        actions={
          <Button variant="outline" size="sm" loading={leaving} onClick={leave}>
            <LogOut className="size-3.5" /> Leave organization
          </Button>
        }
      />

      <div className="mb-5 grid grid-cols-1 gap-4 sm:grid-cols-3">
        <Card>
          <CardHeader className="pb-2">
            <CardDescription className="flex items-center gap-1.5 font-mono text-[10.5px] uppercase tracking-[0.12em]">
              <Users className="size-3.5" /> Seats used
            </CardDescription>
          </CardHeader>
          <CardContent>
            <span className="tabular text-[2.25rem] font-semibold leading-none tracking-tight">
              {org.seats_used} / {org.seats_limit}
            </span>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription className="flex items-center gap-1.5 font-mono text-[10.5px] uppercase tracking-[0.12em]">
              <CreditCard className="size-3.5" /> Plan
            </CardDescription>
          </CardHeader>
          <CardContent>
            <span className="text-[2.25rem] font-semibold capitalize leading-none tracking-tight">{org.plan}</span>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription className="flex items-center gap-1.5 font-mono text-[10.5px] uppercase tracking-[0.12em]">
              <ShieldCheck className="size-3.5" /> Your role
            </CardDescription>
          </CardHeader>
          <CardContent>
            <span className="text-[2.25rem] font-semibold capitalize leading-none tracking-tight">{orgRole}</span>
          </CardContent>
        </Card>
      </div>

      {isAdmin ? (
        <InviteCard
          seatsAvailable={org.seats_used < org.seats_limit}
          onInvited={(invite) => {
            setMintedInvite(invite);
            void invites.refresh();
            void members.refresh();
          }}
        />
      ) : null}

      <Card className="mb-5">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Users className="size-4 text-muted-foreground" aria-hidden="true" /> Members
          </CardTitle>
          <CardDescription>
            {isAdmin ? "Usage figures are visible to owners and admins only." : "Only owners and admins can see usage figures."}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          {!members.data ? (
            <>
              <Skeleton className="h-10" />
              <Skeleton className="h-10" />
            </>
          ) : (
            members.data.map((member) => (
              <MemberRow
                key={member.user_id}
                member={member}
                isAdmin={isAdmin}
                isSelf={member.user_id === account!.id}
                onChanged={members.refresh}
              />
            ))
          )}
        </CardContent>
      </Card>

      {isAdmin ? (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Mail className="size-4 text-muted-foreground" aria-hidden="true" /> Pending invites
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {!invites.data || invites.data.length === 0 ? (
              <p className="text-xs text-muted-foreground">No pending invites.</p>
            ) : (
              invites.data.map((invite) => <InviteRow key={invite.id} invite={invite} onChanged={invites.refresh} />)
            )}
          </CardContent>
        </Card>
      ) : null}

      <InviteLinkDialog invite={mintedInvite} onClose={() => setMintedInvite(null)} />
    </>
  );
}

function InviteCard({ onInvited, seatsAvailable }: { onInvited: (invite: OrganizationInvite) => void; seatsAvailable: boolean }) {
  const toast = useToast();
  const [email, setEmail] = React.useState("");
  const [role, setRole] = React.useState<OrganizationRole>("member");
  const [busy, setBusy] = React.useState(false);

  async function invite() {
    if (!email.trim()) return;
    setBusy(true);
    try {
      const row = await api.inviteMember(email.trim(), role);
      setEmail("");
      onInvited(row);
    } catch (err) {
      toast.push({ title: "Could not invite", description: errorMessage(err), variant: "error" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className="mb-5">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <UserPlus className="size-4 text-muted-foreground" aria-hidden="true" /> Invite a teammate
        </CardTitle>
        <CardDescription>
          {seatsAvailable
            ? "Generates a one-time link to send however you like - Slack, email, text."
            : "No seats left. Remove a member or raise the seat limit before inviting another."}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-wrap items-end gap-2">
        <div className="min-w-48 flex-1 space-y-1.5">
          <Label htmlFor="invite-email">Email</Label>
          <Input
            id="invite-email"
            type="email"
            placeholder="teammate@company.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void invite()}
          />
        </div>
        <div className="w-36 space-y-1.5">
          <Label htmlFor="invite-role">Role</Label>
          <Select id="invite-role" value={role} onChange={(e) => setRole(e.target.value as OrganizationRole)}>
            <option value="member">Member</option>
            <option value="admin">Admin</option>
          </Select>
        </div>
        <Button loading={busy} disabled={!email.trim() || !seatsAvailable} onClick={invite}>
          <Mail className="size-3.5" /> Send invite
        </Button>
      </CardContent>
    </Card>
  );
}

/** Shared avatar treatment for member/invite rows - initials circle, or an icon for a row with no name yet. */
function RowAvatar({ initials, icon }: { initials?: string; icon?: React.ReactNode }) {
  return (
    <div
      className={cn(
        "grid size-9 shrink-0 place-items-center rounded-full text-xs font-semibold",
        initials
          ? "bg-[linear-gradient(140deg,var(--primary),color-mix(in_oklch,var(--primary)_55%,white))] text-primary-foreground"
          : "bg-muted text-muted-foreground",
      )}
      aria-hidden="true"
    >
      {initials ?? icon}
    </div>
  );
}

function MemberRow({
  member,
  isAdmin,
  isSelf,
  onChanged,
}: {
  member: OrganizationMember;
  isAdmin: boolean;
  isSelf: boolean;
  onChanged: () => void;
}) {
  const toast = useToast();
  const [busy, setBusy] = React.useState(false);

  async function setRole(role: string) {
    setBusy(true);
    try {
      await api.setMemberRole(member.user_id, role);
      onChanged();
    } catch (err) {
      toast.push({ title: "Could not change role", description: errorMessage(err), variant: "error" });
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    setBusy(true);
    try {
      await api.removeMember(member.user_id);
      toast.push({ title: `Removed ${member.display_name}` });
      onChanged();
    } catch (err) {
      toast.push({ title: "Could not remove member", description: errorMessage(err), variant: "error" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-3 rounded-lg border border-border px-3 py-2.5 transition-colors hover:bg-accent/30">
      <RowAvatar initials={member.display_name.trim().slice(0, 2).toUpperCase() || "?"} />
      <div className="min-w-40 flex-1">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium">{member.display_name}</span>
          {isSelf ? <Badge variant="outline">You</Badge> : null}
        </div>
        <p className="truncate text-xs text-muted-foreground">{member.email}</p>
      </div>
      {isAdmin && member.meetings_this_month !== null && member.meetings_this_month !== undefined ? (
        <div className="hidden gap-4 border-l border-border pl-4 sm:flex">
          <Stat label="Meetings" value={member.meetings_this_month} className="w-16" />
          <Stat label="Cost" value={`$${(member.estimated_cost_usd ?? 0).toFixed(2)}`} className="w-16" />
        </div>
      ) : null}
      {isAdmin && !isSelf ? (
        <Select
          className="w-32"
          value={member.role}
          disabled={busy}
          onChange={(e) => void setRole(e.target.value)}
        >
          <option value="member">Member</option>
          <option value="admin">Admin</option>
          <option value="owner">Owner</option>
        </Select>
      ) : (
        <Badge variant={ROLE_BADGE[member.role]}>{member.role}</Badge>
      )}
      {isAdmin && !isSelf ? (
        <Button variant="ghost" size="icon" loading={busy} onClick={remove} aria-label={`Remove ${member.display_name}`}>
          <Trash2 className="size-4" />
        </Button>
      ) : null}
    </div>
  );
}

function InviteRow({ invite, onChanged }: { invite: OrganizationInvite; onChanged: () => void }) {
  const toast = useToast();
  const [busy, setBusy] = React.useState(false);
  const [minted, setMinted] = React.useState<OrganizationInvite | null>(null);

  async function resend() {
    setBusy(true);
    try {
      const fresh = await api.resendInvite(invite.id);
      setMinted(fresh);
      onChanged();
    } catch (err) {
      toast.push({ title: "Could not resend", description: errorMessage(err), variant: "error" });
    } finally {
      setBusy(false);
    }
  }

  async function revoke() {
    setBusy(true);
    try {
      await api.revokeInvite(invite.id);
      onChanged();
    } catch (err) {
      toast.push({ title: "Could not revoke", description: errorMessage(err), variant: "error" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="flex flex-wrap items-center gap-3 rounded-lg border border-border px-3 py-2.5 transition-colors hover:bg-accent/30">
        <RowAvatar icon={<Mail className="size-4" aria-hidden="true" />} />
        <div className="min-w-40 flex-1">
          <p className="text-sm font-medium">{invite.email}</p>
          <p className="text-xs text-muted-foreground">
            Expires {new Date(invite.expires_at).toLocaleDateString()}
          </p>
        </div>
        <Badge variant={ROLE_BADGE[invite.role]}>{invite.role}</Badge>
        <Button variant="outline" size="sm" loading={busy} onClick={resend}>
          Resend
        </Button>
        <Button variant="ghost" size="sm" loading={busy} onClick={revoke}>
          Revoke
        </Button>
      </div>
      <InviteLinkDialog invite={minted} onClose={() => setMinted(null)} />
    </>
  );
}

function InviteLinkDialog({ invite, onClose }: { invite: OrganizationInvite | null; onClose: () => void }) {
  const [copied, setCopied] = React.useState(false);
  const url = invite?.invite_url ? `${window.location.origin}${invite.invite_url}` : "";

  React.useEffect(() => setCopied(false), [invite]);

  async function copy() {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
    } catch {
      /* clipboard denied; the field below can still be selected and copied by hand */
    }
  }

  return (
    <Dialog
      open={invite !== null}
      onClose={onClose}
      title="Invite link ready"
      description={invite ? `Send this to ${invite.email} however you like - it works once.` : undefined}
      footer={<Button onClick={onClose}>Done</Button>}
    >
      <div className="flex items-center gap-2">
        <Input readOnly value={url} onFocus={(e) => e.currentTarget.select()} />
        <Button variant="outline" size="icon" onClick={copy} aria-label="Copy invite link">
          {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
        </Button>
      </div>
    </Dialog>
  );
}
