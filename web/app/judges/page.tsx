import type { Metadata } from "next";
import Link from "next/link";
import { JudgeEntry } from "@/components/auth/judge-entry";
import { APP_NAME } from "@/lib/config";

export const metadata: Metadata = { title: "Judge guide" };

const STEPS: { title: string; body: string; href?: string; cta?: string }[] = [
  {
    title: "Press “Get straight to the product”",
    body: "One click creates a private demo workspace and opens the Playground. Sample documents (a profile, a project brief, a team calendar) are indexed for you in the background. Prefer your own account? Sign up normally; nothing else changes.",
  },
  {
    title: "Run “Hold the line on a negotiation”",
    body: "A customer asks for 20 percent off. The delegate refuses to improvise on money, defers out loud and asks “your phone”. An approval card appears on the right.",
  },
  {
    title: "Answer from the phone",
    body: "Approve, decline politely or soften. Watch the delegate say exactly what you chose. In production this is a Telegram button; here it is a button on the page.",
  },
  {
    title: "Click any line to see why it did that",
    body: "The inspector shows the checks it ran (was it for me? what policy tier? which passage grounded it? did the output guard pass it?), the model latency and the evidence it quoted.",
  },
  {
    title: "Try to break it",
    body: "Run “Try to make it hallucinate”, or type your own: an unanswerable question, a salary question, a prompt injection. It must not invent, disclose or obey.",
  },
  {
    title: "End the meeting and read the briefing",
    body: "You get an executive briefing and the action items the delegate was handed (the commitment ledger). They also land in Briefs and Commitments.",
    href: "/history",
    cta: "Briefs",
  },
];

const PARTS: [string, string][] = [
  ["Policy router brain", "Every utterance is classified, checked against your policy, and answered, deferred or escalated."],
  ["Knowledge base (RAG)", "Per-tenant ChromaDB index, local embeddings, a dossier compiled from your files."],
  ["Output guard", "Each factual claim is verified against its source before it is spoken; restricted topics are re-checked on the answer."],
  ["Commitment ledger", "Action items are assembled across several utterances, acknowledged, and tracked."],
  ["Multi-tenancy", "Accounts, per-user keys encrypted at rest, usage metering, consent audit trail, teams."],
  ["Honest failure", "A model that stops answering shows a red banner; the delegate never silently degrades."],
];

export default function JudgesPage() {
  return (
    <main className="dark min-h-screen bg-[linear-gradient(175deg,#17161c_0%,#0c0c0e_55%,#080808_100%)] px-4 py-14 text-foreground">
      <div className="mx-auto max-w-3xl space-y-12">
        <header className="space-y-4">
          <span className="grid size-10 place-items-center rounded-xl bg-primary text-sm font-bold text-primary-foreground">OO</span>
          <h1 className="text-3xl font-semibold tracking-tight">{APP_NAME}: an AI delegate that attends your meetings</h1>
          <p className="text-base leading-relaxed text-muted-foreground">
            You can&apos;t be in every meeting. {APP_NAME} joins on your behalf, speaks only what your policy allows, answers from your own
            documents, asks you on your phone before saying anything sensitive, and sends you a briefing with the commitments it made.
          </p>
          <JudgeEntry className="max-w-md pt-2" />
          <p className="text-xs text-muted-foreground">
            Or{" "}
            <Link href="/signup" className="underline underline-offset-2">
              create an account
            </Link>{" "}
            /{" "}
            <Link href="/login" className="underline underline-offset-2">
              sign in
            </Link>
            .
          </p>
        </header>

        <section className="space-y-4">
          <h2 className="text-lg font-semibold">Try it in about 2 minutes</h2>
          <ol className="space-y-3">
            {STEPS.map((step, index) => (
              <li key={step.title} className="flex gap-4 rounded-xl border border-border bg-card p-4">
                <span className="grid size-7 shrink-0 place-items-center rounded-full bg-primary/15 font-mono text-xs text-primary">{index + 1}</span>
                <div className="space-y-1">
                  <h3 className="text-sm font-medium">{step.title}</h3>
                  <p className="text-sm leading-relaxed text-muted-foreground">{step.body}</p>
                  {step.href ? (
                    <Link href={step.href} className="inline-block pt-1 text-sm text-primary underline underline-offset-2">
                      {step.cta} →
                    </Link>
                  ) : null}
                </div>
              </li>
            ))}
          </ol>
        </section>

        <section className="space-y-3 rounded-xl border border-border bg-card p-5">
          <h2 className="text-lg font-semibold">What you are and aren&apos;t seeing</h2>
          <p className="text-sm leading-relaxed text-muted-foreground">
            The Playground is <strong className="text-foreground">not a mock</strong>. Each turn runs through the same code a live call uses
            (floor check, policy sensitivity, response planner, output guard, meeting memory, commitment ledger) over your own knowledge base.
            Only the two edges differ: speech arrives as text instead of audio, and approval is a button instead of Telegram.
          </p>
          <p className="text-sm leading-relaxed text-muted-foreground">
            Joining a real Google Meet, Zoom or Teams call needs a browser with virtual audio devices, so that part runs in a worker container
            or on a presenter&apos;s machine rather than on this serverless host. It is shown in the live demo.
          </p>
          <p className="text-sm leading-relaxed text-muted-foreground">
            Demo workspaces are throwaway, rate-limited and use a small shared quota. If the first request is slow, the instance is waking up.
          </p>
        </section>

        <section className="space-y-4">
          <h2 className="text-lg font-semibold">What is inside</h2>
          <dl className="grid gap-3 sm:grid-cols-2">
            {PARTS.map(([name, text]) => (
              <div key={name} className="rounded-xl border border-border bg-card p-4">
                <dt className="text-sm font-medium">{name}</dt>
                <dd className="mt-1 text-sm leading-relaxed text-muted-foreground">{text}</dd>
              </div>
            ))}
          </dl>
        </section>
      </div>
    </main>
  );
}
