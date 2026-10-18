"use client";

import * as React from "react";
import { ArrowRightLeft, Ban, CheckCircle2, Hand, Pencil, Plus, RotateCcw, Save, Sparkles, Trash2, Wand2 } from "lucide-react";
import { api, errorMessage } from "@/lib/api";
import type { KnowledgeEntry, PolicyRule, PolicyTier, UserProfile } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useApi } from "@/hooks/use-api";
import { useToast } from "@/components/toast";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { PageHeader } from "@/components/page-header";
import { TierDot } from "@/components/status-badge";
import { ChipInput } from "@/components/policy/chip-input";
import { RuleDialog, TIER_META } from "@/components/policy/rule-dialog";
import { emptyRule, parseRuleText } from "@/components/policy/rule-parser";

const TIERS: PolicyTier[] = ["GREEN", "YELLOW", "RED"];
const TIER_ICON: Record<PolicyTier, typeof CheckCircle2> = { GREEN: CheckCircle2, YELLOW: Hand, RED: Ban };

interface EditorState {
  mode: "create" | "edit";
  rule: PolicyRule;
  facts: KnowledgeEntry[];
}

export function PolicyEditor() {
  const remote = useApi<UserProfile>(api.profile);
  const toast = useToast();
  const [draft, setDraft] = React.useState<UserProfile | null>(null);
  const [editor, setEditor] = React.useState<EditorState | null>(null);
  const [pendingDelete, setPendingDelete] = React.useState<PolicyRule | null>(null);
  const [quick, setQuick] = React.useState("");
  const [saving, setSaving] = React.useState(false);
  const [saveError, setSaveError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (remote.data && !draft) setDraft(remote.data);
  }, [remote.data, draft]);

  const dirty = Boolean(draft && remote.data && JSON.stringify(draft) !== JSON.stringify(remote.data));

  function update(fn: (profile: UserProfile) => UserProfile) {
    setDraft((current) => (current ? fn(current) : current));
    setSaveError(null);
  }

  function openCreate(tier: PolicyTier, prefill?: Partial<PolicyRule>) {
    setEditor({ mode: "create", rule: { ...emptyRule(tier), ...prefill }, facts: [] });
  }

  function openEdit(rule: PolicyRule) {
    if (!draft) return;
    setEditor({ mode: "edit", rule, facts: draft.knowledge_base.filter((f) => f.topic === rule.topic) });
  }

  function saveRule(rule: PolicyRule, facts: KnowledgeEntry[]) {
    if (!editor) return;
    update((profile) => {
      const others = profile.policy.filter((r) => r.topic !== editor.rule.topic);
      const position = profile.policy.findIndex((r) => r.topic === editor.rule.topic);
      const policy = editor.mode === "edit" && position !== -1 ? [...profile.policy.slice(0, position), rule, ...profile.policy.slice(position + 1)] : [...others, rule];
      const knowledge_base = [...profile.knowledge_base.filter((f) => f.topic !== editor.rule.topic), ...facts];
      return { ...profile, policy, knowledge_base };
    });
    setEditor(null);
  }

  function moveTier(rule: PolicyRule, tier: PolicyTier) {
    if (tier === rule.tier) return;
    update((profile) => ({
      ...profile,
      policy: profile.policy.map((r) => (r.topic === rule.topic ? { ...r, tier } : r)),
      knowledge_base: tier === "GREEN" ? profile.knowledge_base : profile.knowledge_base.filter((f) => f.topic !== rule.topic),
    }));
  }

  function deleteRule(rule: PolicyRule) {
    update((profile) => ({
      ...profile,
      policy: profile.policy.filter((r) => r.topic !== rule.topic),
      knowledge_base: profile.knowledge_base.filter((f) => f.topic !== rule.topic),
    }));
    setPendingDelete(null);
  }

  function quickAdd(event: React.FormEvent) {
    event.preventDefault();
    const parsed = parseRuleText(quick);
    if (!parsed) return;
    const taken = new Set(draft?.policy.map((r) => r.topic) ?? []);
    let topic = parsed.topic;
    let n = 2;
    while (taken.has(topic)) topic = `${parsed.topic}_${n++}`;
    openCreate(parsed.tier, { description: parsed.description, topic, triggers: parsed.triggers });
    setQuick("");
  }

  async function save() {
    if (!draft) return;
    setSaving(true);
    setSaveError(null);
    try {
      const saved = await api.saveProfile(draft);
      setDraft(saved);
      await remote.refresh();
      toast.push({ title: "Representation Policy saved", description: `${saved.policy.length} rules and ${saved.knowledge_base.length} facts saved. They apply from the next thing said.`, variant: "success" });
    } catch (err) {
      const message = errorMessage(err);
      setSaveError(message);
      toast.push({ title: "Could not save the policy", description: message, variant: "error" });
    } finally {
      setSaving(false);
    }
  }

  const existingTopics = draft?.policy.map((r) => r.topic) ?? [];

  return (
    <>
      <PageHeader
        title="Representation Policy"
        description="What the delegate may answer on its own, what it must clear with you first, and what it must never touch."
      />

      {remote.error && !draft ? <div className="mb-4 rounded-md border border-danger/40 bg-danger/5 px-4 py-3 text-sm text-danger">{remote.error}</div> : null}
      {saveError ? <div className="mb-4 rounded-md border border-danger/40 bg-danger/5 px-4 py-3 text-sm text-danger">{saveError}</div> : null}

      {!draft ? (
        <div className="grid gap-4 lg:grid-cols-3">
          {TIERS.map((t) => (
            <Skeleton key={t} className="h-64" />
          ))}
        </div>
      ) : (
        <div className="flex flex-col gap-4">
          <div className="grid gap-3 sm:grid-cols-3">
            {TIERS.map((t) => {
              const Icon = TIER_ICON[t];
              const count = draft.policy.filter((r) => r.tier === t).length;
              return (
                <div
                  key={t}
                  className={cn(
                    "flex gap-3.5 rounded-xl border px-4 py-3.5",
                    t === "GREEN" ? "border-success/30 bg-success/10" : t === "YELLOW" ? "border-warning/30 bg-warning/10" : "border-danger/30 bg-danger/10",
                  )}
                >
                  <div
                    className={cn(
                      "flex size-10 shrink-0 items-center justify-center rounded-full text-white",
                      t === "GREEN" ? "bg-success" : t === "YELLOW" ? "bg-warning" : "bg-danger",
                    )}
                  >
                    <Icon className="size-5" />
                  </div>
                  <div className="min-w-0">
                    <div className="flex items-baseline gap-2">
                      <span className="text-sm font-semibold">{TIER_META[t].name}</span>
                      <span className="tabular font-mono text-xs text-muted-foreground">{count}</span>
                    </div>
                    <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">{TIER_META[t].blurb}</p>
                  </div>
                </div>
              );
            })}
          </div>

          {dirty ? (
            <div className="flex flex-wrap items-center gap-3 rounded-xl border border-primary/30 bg-primary/10 px-4 py-2.5">
              <Badge className="h-6">
                <Pencil className="size-3" /> Unsaved changes
              </Badge>
              <span className="flex-1 text-xs text-primary">Live delegates keep using the saved policy until you save.</span>
              <Button variant="outline" size="sm" onClick={() => remote.data && setDraft(remote.data)} disabled={saving}>
                <RotateCcw className="size-3.5" /> Discard
              </Button>
              <Button size="sm" onClick={save} loading={saving}>
                <Save className="size-3.5" /> Save policy
              </Button>
            </div>
          ) : null}

          <div className="grid gap-4 lg:grid-cols-3">
            <Card className="lg:col-span-1">
              <CardHeader className="pb-3">
                <CardTitle>Principal</CardTitle>
                <CardDescription>Who the delegate speaks for. It stays silent when this person speaks or when someone else is addressed.</CardDescription>
              </CardHeader>
              <CardContent className="flex flex-col gap-3">
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="display-name">Display name</Label>
                  <Input id="display-name" value={draft.display_name} onChange={(e) => update((p) => ({ ...p, display_name: e.target.value }))} />
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="aliases">Also addressed as</Label>
                  <ChipInput id="aliases" value={draft.aliases} onChange={(aliases) => update((p) => ({ ...p, aliases }))} placeholder="Durai, DK" />
                </div>
                <p className="text-xs text-muted-foreground">
                  id <span className="font-mono">{draft.user_id}</span> · {draft.policy.length} rules · {draft.knowledge_base.length} grounded facts
                </p>
              </CardContent>
            </Card>

            <Card className="lg:col-span-2">
              <CardHeader className="pb-3">
                <CardTitle className="flex items-center gap-2">
                  <Sparkles className="size-4 text-muted-foreground" /> Add a rule in plain English
                </CardTitle>
                <CardDescription>Start with the verb and the tier follows: “Allow discussing Q3 roadmap”, “Confirm with me before any deadline”, “Escalate any pricing discussions”.</CardDescription>
              </CardHeader>
              <CardContent>
                <form onSubmit={quickAdd} className="flex flex-col gap-2 sm:flex-row">
                  <Input value={quick} onChange={(e) => setQuick(e.target.value)} placeholder="Escalate any pricing discussions" aria-label="Describe a rule" />
                  <Button type="submit" disabled={!quick.trim()} className="sm:w-40">
                    <Wand2 className="size-4" /> Draft rule
                  </Button>
                </form>
                <QuickPreview text={quick} />
              </CardContent>
            </Card>
          </div>

          <div className="grid gap-4 lg:grid-cols-3">
            {TIERS.map((tier) => (
              <TierColumn
                key={tier}
                tier={tier}
                rules={draft.policy.filter((r) => r.tier === tier)}
                facts={draft.knowledge_base}
                onAdd={() => openCreate(tier)}
                onEdit={openEdit}
                onMove={moveTier}
                onDelete={(rule) => setPendingDelete(rule)}
              />
            ))}
          </div>
        </div>
      )}

      {editor ? (
        <RuleDialog
          open
          mode={editor.mode}
          initial={editor.rule}
          initialFacts={editor.facts}
          existingTopics={existingTopics}
          onClose={() => setEditor(null)}
          onSave={saveRule}
        />
      ) : null}

      <Dialog
        open={Boolean(pendingDelete)}
        onClose={() => setPendingDelete(null)}
        title="Remove this rule?"
        footer={
          <>
            <Button variant="outline" onClick={() => setPendingDelete(null)}>Cancel</Button>
            <Button variant="destructive" onClick={() => pendingDelete && deleteRule(pendingDelete)}>
              <Trash2 className="size-4" /> Remove
            </Button>
          </>
        }
      >
        {pendingDelete ? (
          <p className="text-sm text-muted-foreground">
            <span className="font-mono text-foreground">{pendingDelete.topic}</span> and its {pendingDelete.triggers.length} trigger phrases go away.
            {(() => {
              const n = draft?.knowledge_base.filter((f) => f.topic === pendingDelete.topic).length ?? 0;
              return n ? ` ${n} grounded fact${n === 1 ? "" : "s"} attached to it will be removed too.` : "";
            })()}{" "}
            Utterances on this topic will then fall through to the default: silent unless addressed directly, escalated when they are.
          </p>
        ) : null}
      </Dialog>
    </>
  );
}

function QuickPreview({ text }: { text: string }) {
  const parsed = parseRuleText(text);
  if (!parsed || !text.trim()) return <p className="mt-2 text-xs text-muted-foreground">Verbs like allow / answer make it Green, confirm / ask me make it Yellow, escalate / never make it Red. You review the draft before it is added.</p>;
  return (
    <p className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
      <span className="inline-flex items-center gap-1.5 font-medium text-foreground">
        <TierDot tier={parsed.tier} /> {TIER_META[parsed.tier].name}
      </span>
      <span>topic <span className="font-mono text-foreground">{parsed.topic}</span></span>
      <span>triggers {parsed.triggers.map((t) => <code key={t} className="ml-1 rounded bg-muted px-1 py-0.5 text-[11px] text-foreground">{t}</code>)}</span>
    </p>
  );
}

function TierColumn({
  tier,
  rules,
  facts,
  onAdd,
  onEdit,
  onMove,
  onDelete,
}: {
  tier: PolicyTier;
  rules: PolicyRule[];
  facts: KnowledgeEntry[];
  onAdd: () => void;
  onEdit: (rule: PolicyRule) => void;
  onMove: (rule: PolicyRule, tier: PolicyTier) => void;
  onDelete: (rule: PolicyRule) => void;
}) {
  const meta = TIER_META[tier];
  const accent = tier === "GREEN" ? "border-t-success" : tier === "YELLOW" ? "border-t-warning" : "border-t-danger";
  return (
    <Card className={cn("flex flex-col border-t-2", accent)}>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2 text-base">
            <TierDot tier={tier} className="size-2.5" /> {meta.name}
            <Badge variant="outline" className="tabular">{rules.length}</Badge>
          </CardTitle>
          <Button variant="outline" size="sm" onClick={onAdd}>
            <Plus className="size-3.5" /> Add
          </Button>
        </div>
        <CardDescription>
          <span className="font-medium text-foreground">{meta.verb}.</span> {meta.blurb}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-1 flex-col gap-2.5">
        {rules.length === 0 ? (
          <div className="rounded-md border border-dashed border-border p-4 text-center text-xs text-muted-foreground">No {meta.name.toLowerCase()} rules.</div>
        ) : (
          rules.map((rule) => <RuleCard key={rule.topic} rule={rule} factCount={facts.filter((f) => f.topic === rule.topic).length} onEdit={() => onEdit(rule)} onMove={(t) => onMove(rule, t)} onDelete={() => onDelete(rule)} />)
        )}
      </CardContent>
    </Card>
  );
}

function RuleCard({ rule, factCount, onEdit, onMove, onDelete }: { rule: PolicyRule; factCount: number; onEdit: () => void; onMove: (tier: PolicyTier) => void; onDelete: () => void }) {
  const [showAll, setShowAll] = React.useState(false);
  const shown = showAll ? rule.triggers : rule.triggers.slice(0, 6);
  const hidden = rule.triggers.length - shown.length;
  return (
    <div className="group rounded-md border border-border bg-card p-3 transition-colors hover:border-foreground/20">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-sm font-medium leading-snug">{rule.description}</p>
          <p className="mt-0.5 font-mono text-[11px] text-muted-foreground">
            {rule.topic}
            {rule.tier === "GREEN" ? <span className="ml-2 font-sans">{factCount ? `${factCount} fact${factCount === 1 ? "" : "s"}` : rule.topic === "documented_facts" ? "answers from documents" : "documents only"}</span> : null}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-0.5 opacity-70 transition-opacity group-hover:opacity-100">
          <Button variant="ghost" size="icon" className="size-7" onClick={onEdit} aria-label={`Edit ${rule.topic}`}>
            <Pencil className="size-3.5" />
          </Button>
          <Button variant="ghost" size="icon" className="size-7 hover:text-danger" onClick={onDelete} aria-label={`Delete ${rule.topic}`}>
            <Trash2 className="size-3.5" />
          </Button>
        </div>
      </div>
      <div className="mt-2 flex flex-wrap gap-1">
        {shown.map((t) => (
          <span key={t} className="rounded bg-muted px-1.5 py-0.5 text-[11px] leading-4">{t}</span>
        ))}
        {hidden > 0 ? (
          <button type="button" onClick={() => setShowAll(true)} className="rounded px-1.5 py-0.5 text-[11px] text-muted-foreground hover:text-foreground">
            +{hidden} more
          </button>
        ) : null}
      </div>
      <div className="mt-2.5 flex items-center gap-2 text-[11px] text-muted-foreground">
        <ArrowRightLeft className="size-3" />
        <label className="sr-only" htmlFor={`move-${rule.topic}`}>Move {rule.topic} to tier</label>
        <Select id={`move-${rule.topic}`} value={rule.tier} onChange={(e) => onMove(e.target.value as PolicyTier)} className="h-7 w-auto py-0 pl-2 pr-7 text-[11px]">
          {TIERS.map((t) => (
            <option key={t} value={t}>
              {TIER_META[t].name} · {TIER_META[t].verb.toLowerCase()}
            </option>
          ))}
        </Select>
      </div>
    </div>
  );
}
