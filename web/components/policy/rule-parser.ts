import type { PolicyRule, PolicyTier } from "@/lib/types";
import { slugify } from "@/lib/utils";

const RED_VERBS = /^(escalate|never|block|refuse|reject|forbid|do not|don't|dont|no |red\b|always escalate|flag)/i;
const YELLOW_VERBS = /^(confirm|approve|ask me|ask before|check with me|verify|yellow\b|require approval|get approval|clear with me|run .* by me)/i;
const GREEN_VERBS = /^(allow|answer|discuss|share|explain|handle|green\b|ok to|okay to|can talk|talk about|cover|respond to)/i;

const LEADING = /^(escalate|never|block|refuse|reject|forbid|do not|don't|dont|no|always escalate|flag|confirm|approve|ask me|ask before|check with me|verify|require approval|get approval|clear with me|allow|answer|discuss|share|explain|handle|ok to|okay to|can talk|talk about|cover|respond to|red|yellow|green)\b/i;
const FILLER = new Set([
  "a", "an", "the", "any", "all", "every", "about", "of", "on", "for", "to", "with", "regarding", "around", "our", "my", "their", "this", "that",
  "discussing", "discussion", "discussions", "talking", "talk", "questions", "question", "topic", "topics", "matters", "issues", "related", "anything", "something",
  "before", "committing", "commit", "first", "me", "please", "always", "ever", "when", "asked", "if", "someone", "they", "we",
]);

export interface ParsedRule {
  tier: PolicyTier;
  description: string;
  topic: string;
  triggers: string[];
  phrase: string;
}

function tidy(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

/** Turn "Escalate any pricing discussions" into a RED rule with topic `pricing` and triggers ["pricing discussions", "pricing"]. */
export function parseRuleText(input: string): ParsedRule | null {
  const text = tidy(input);
  if (!text) return null;
  const tier: PolicyTier = RED_VERBS.test(text) ? "RED" : YELLOW_VERBS.test(text) ? "YELLOW" : GREEN_VERBS.test(text) ? "GREEN" : /escalat|never|refuse/i.test(text) ? "RED" : /approv|confirm|check/i.test(text) ? "YELLOW" : "GREEN";

  let rest = text.replace(LEADING, "").replace(/^[\s,:-]+/, "");
  rest = rest.replace(/[.!?]+$/, "");
  const words = rest.split(" ").filter(Boolean);
  const content = words.filter((w) => !FILLER.has(w.toLowerCase().replace(/[^a-z0-9%'-]/g, "")));
  const phrase = tidy(content.join(" ")).toLowerCase() || tidy(rest).toLowerCase();

  const triggers = new Set<string>();
  if (phrase) triggers.add(phrase);
  for (const word of content) {
    const clean = word.toLowerCase().replace(/[^a-z0-9%'-]/g, "");
    if (clean.length > 3 && clean !== phrase) triggers.add(clean);
  }
  const description = text.replace(/[.!?]*$/, "").replace(/^./, (c) => c.toUpperCase()) + ".";
  return { tier, description, topic: slugify(phrase || description), triggers: [...triggers].slice(0, 8), phrase };
}

export function normaliseTrigger(raw: string): string {
  return raw.trim().toLowerCase().replace(/\s+/g, " ");
}

export const TOPIC_RE = /^[a-z][a-z0-9_]*$/;
export const SUBJECT_RE = /^[a-z][a-z0-9_]*$/;

export function emptyRule(tier: PolicyTier): PolicyRule {
  return { topic: "", tier, description: "", triggers: [] };
}
