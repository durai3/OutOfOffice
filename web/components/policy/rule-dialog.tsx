"use client";

import * as React from "react";
import { Plus, Trash2 } from "lucide-react";
import type { KnowledgeEntry, PolicyRule, PolicyTier } from "@/lib/types";
import { cn, slugify } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Hint, Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { TierDot } from "@/components/status-badge";
import { ChipInput } from "@/components/policy/chip-input";
import { SUBJECT_RE, TOPIC_RE, normaliseTrigger } from "@/components/policy/rule-parser";

export const TIER_META: Record<PolicyTier, { name: string; verb: string; blurb: string }> = {
  GREEN: { name: "Green", verb: "Answer autonomously", blurb: "The delegate answers on its own, but only with a grounded fact from this profile or the document index. No fact, no answer: it escalates instead." },
  YELLOW: { name: "Yellow", verb: "Ask before committing", blurb: "A request on this topic pauses the delegate and pings Telegram. It speaks only after you tap Approve, Reject, or Decline Politely." },
  RED: { name: "Red", verb: "Never handle", blurb: "The moment the topic is raised, even as a statement, the delegate escalates and says nothing until you decide." },
};

interface RuleDialogProps {
  open: boolean;
  onClose: () => void;
  onSave: (rule: PolicyRule, facts: KnowledgeEntry[]) => void;
  initial: PolicyRule;
  initialFacts: KnowledgeEntry[];
  existingTopics: string[];
  mode: "create" | "edit";
}

interface FactDraft {
  key: number;
  subject: string;
  keywords: string[];
  answer: string;
}

export function RuleDialog({ open, onClose, onSave, initial, initialFacts, existingTopics, mode }: RuleDialogProps) {
  // The parent mounts a fresh dialog per open, so state initialises straight from the props.
  const [description, setDescription] = React.useState(initial.description);
  const [topic, setTopic] = React.useState(initial.topic);
  const [topicTouched, setTopicTouched] = React.useState(mode === "edit" || Boolean(initial.topic));
  const [tier, setTier] = React.useState<PolicyTier>(initial.tier);
  const [triggers, setTriggers] = React.useState<string[]>(initial.triggers);
  const [facts, setFacts] = React.useState<FactDraft[]>(() => initialFacts.map((f, i) => ({ key: i, subject: f.subject, keywords: f.keywords, answer: f.answer })));
  const [attempted, setAttempted] = React.useState(false);
  const factKey = React.useRef(initialFacts.length);

  React.useEffect(() => {
    if (!topicTouched) setTopic(description.trim() ? slugify(description) : "");
  }, [description, topicTouched]);

  const topicTaken = existingTopics.includes(topic) && topic !== initial.topic;
  const topicInvalid = !TOPIC_RE.test(topic);
  const errors = {
    description: description.trim() ? null : "Describe what this rule covers.",
    topic: topicInvalid ? "Use snake_case: letters, digits and underscores, starting with a letter." : topicTaken ? "Another rule already uses this topic id." : null,
    triggers: triggers.length ? null : "Add at least one phrase that identifies the topic in speech.",
    facts: facts.some((f) => !f.subject.trim() || !SUBJECT_RE.test(f.subject) || !f.keywords.length || !f.answer.trim())
      ? "Every fact needs a snake_case subject, at least one keyword, and an answer."
      : null,
  };
  const valid = !errors.description && !errors.topic && !errors.triggers && (tier !== "GREEN" || !errors.facts);

  function submit() {
    setAttempted(true);
    if (!valid) return;
    const rule: PolicyRule = { topic, tier, description: description.trim(), triggers };
    const entries: KnowledgeEntry[] = tier === "GREEN" ? facts.map((f) => ({ topic, subject: f.subject.trim(), keywords: f.keywords, answer: f.answer.trim() })) : [];
    onSave(rule, entries);
  }

  function addFact() {
    factKey.current += 1;
    setFacts((list) => [...list, { key: factKey.current, subject: "", keywords: [], answer: "" }]);
  }

  return (
    <Dialog
      open={open}
      onClose={onClose}
      size="lg"
      title={mode === "create" ? "New policy rule" : `Edit rule · ${initial.topic}`}
      description="A rule is a topic, the tier that governs it, and the phrases that reveal the topic in conversation."
      footer={
        <>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button onClick={submit} disabled={attempted && !valid}>{mode === "create" ? "Add rule" : "Apply changes"}</Button>
        </>
      }
    >
      <div className="flex flex-col gap-5">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="rule-description">Rule</Label>
          <Input id="rule-description" value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Escalate any pricing discussions." aria-invalid={attempted && Boolean(errors.description)} autoFocus />
          {attempted && errors.description ? <Hint className="text-danger">{errors.description}</Hint> : <Hint>Plain English. This is what you see on Telegram when the rule fires.</Hint>}
        </div>

        <div className="grid gap-4 sm:grid-cols-[1fr_auto]">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="rule-topic">Topic id</Label>
            <Input
              id="rule-topic"
              value={topic}
              onChange={(e) => {
                setTopicTouched(true);
                setTopic(e.target.value.toLowerCase());
              }}
              className="font-mono text-[13px]"
              placeholder="pricing_negotiation"
              aria-invalid={attempted && Boolean(errors.topic)}
              disabled={mode === "edit"}
            />
            {attempted && errors.topic ? <Hint className="text-danger">{errors.topic}</Hint> : <Hint>{mode === "edit" ? "Topic ids are stable; grounded facts reference them." : "Generated from the rule; edit if you like."}</Hint>}
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>Tier</Label>
            <div className="inline-flex rounded-md border border-border p-0.5" role="radiogroup" aria-label="Tier">
              {(["GREEN", "YELLOW", "RED"] as PolicyTier[]).map((t) => (
                <button
                  key={t}
                  type="button"
                  role="radio"
                  aria-checked={tier === t}
                  onClick={() => setTier(t)}
                  className={cn("inline-flex h-8 items-center gap-1.5 rounded px-3 text-xs font-medium transition-colors", tier === t ? "bg-accent text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground")}
                >
                  <TierDot tier={t} /> {TIER_META[t].name}
                </button>
              ))}
            </div>
          </div>
        </div>
        <p className="-mt-2 text-xs text-muted-foreground">
          <span className="font-medium text-foreground">{TIER_META[tier].verb}.</span> {TIER_META[tier].blurb}
        </p>

        <div className="flex flex-col gap-1.5">
          <Label htmlFor="rule-triggers">Trigger phrases</Label>
          <ChipInput id="rule-triggers" value={triggers} onChange={setTriggers} normalise={normaliseTrigger} placeholder="pricing, discount, rate card … press Enter after each" aria-invalid={attempted && Boolean(errors.triggers)} />
          {attempted && errors.triggers ? <Hint className="text-danger">{errors.triggers}</Hint> : <Hint>Matched case-insensitively as whole phrases inside what is said. When several rules match, the most restrictive tier wins.</Hint>}
        </div>

        {tier === "GREEN" ? (
          <div className="flex flex-col gap-3 rounded-md border border-border bg-muted/40 p-4">
            <div className="flex items-start justify-between gap-3">
              <div>
                <p className="text-sm font-medium">Grounded facts</p>
                <p className="text-xs text-muted-foreground">Verbatim answers the delegate may state for this topic. Documents in the Knowledge base are consulted after these.</p>
              </div>
              <Button variant="outline" size="sm" onClick={addFact}>
                <Plus className="size-3.5" /> Add fact
              </Button>
            </div>
            {facts.length === 0 ? <p className="text-xs text-muted-foreground">No curated facts. Questions on this topic are answered from the document index when a confident passage exists, otherwise escalated.</p> : null}
            {facts.map((fact, index) => (
              <div key={fact.key} className="grid gap-3 rounded-md border border-border bg-card p-3 sm:grid-cols-2">
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor={`fact-subject-${fact.key}`}>Subject</Label>
                  <Input
                    id={`fact-subject-${fact.key}`}
                    value={fact.subject}
                    onChange={(e) => setFacts((list) => list.map((f, i) => (i === index ? { ...f, subject: e.target.value.toLowerCase() } : f)))}
                    className="font-mono text-[13px]"
                    placeholder="api_integration"
                    aria-invalid={attempted && (!fact.subject || !SUBJECT_RE.test(fact.subject))}
                  />
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor={`fact-keywords-${fact.key}`}>Keywords</Label>
                  <ChipInput id={`fact-keywords-${fact.key}`} value={fact.keywords} onChange={(keywords) => setFacts((list) => list.map((f, i) => (i === index ? { ...f, keywords } : f)))} normalise={normaliseTrigger} placeholder="api, integration" aria-invalid={attempted && !fact.keywords.length} />
                </div>
                <div className="flex flex-col gap-1.5 sm:col-span-2">
                  <div className="flex items-center justify-between">
                    <Label htmlFor={`fact-answer-${fact.key}`}>Answer (spoken verbatim)</Label>
                    <button type="button" onClick={() => setFacts((list) => list.filter((_, i) => i !== index))} className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-danger">
                      <Trash2 className="size-3" /> remove
                    </button>
                  </div>
                  <Textarea id={`fact-answer-${fact.key}`} value={fact.answer} onChange={(e) => setFacts((list) => list.map((f, i) => (i === index ? { ...f, answer: e.target.value } : f)))} placeholder="The API integration is on track…" className="min-h-[64px]" aria-invalid={attempted && !fact.answer.trim()} />
                </div>
              </div>
            ))}
            {attempted && errors.facts ? <Hint className="text-danger">{errors.facts}</Hint> : null}
          </div>
        ) : initialFacts.length ? (
          <p className="rounded-md border border-warning/40 bg-warning/10 p-3 text-xs text-warning-foreground">
            Moving this rule off Green removes its {initialFacts.length} grounded fact{initialFacts.length === 1 ? "" : "s"}: only Green topics may be answered autonomously.
          </p>
        ) : null}
      </div>
    </Dialog>
  );
}
