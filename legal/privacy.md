---
title: DELEG8 Privacy Policy
version: 2026-09-23.1
effective: 23 September 2026
summary: What DELEG8 collects, where it goes, how long it is kept, and how to get rid of it.
---

# Privacy Policy

**Effective 23 September 2026. Version 2026-09-23.1.**

This policy covers DELEG8 the beta service. It is written to be read, not to be
survived.

## 1. Who this is about

Two groups of people appear in DELEG8:

- **Users** - people with an account, who dispatch a delegate.
- **Meeting participants** - everyone else in a call the delegate joins. They
  do not have an account and did not sign up, which is why the sections about
  them are the strictest ones here.

For meeting participants' data, the DELEG8 user who dispatched the delegate is
the controller and we are the processor. The user decides which meetings are
joined; we only process what that produces.

## 2. What we collect

**From users:**

- Account data: email address, display name, hashed password, sign-in times.
- Configuration: your Representation Policy, your dossier, your notification
  channel (e.g. Telegram chat id), your consent-mode preference.
- Documents you upload and the vector embeddings computed from them.
- Usage metering: token, audio-second and character counts per call, so quotas
  can be enforced. This records *how much*, not *what*.
- Any API keys you choose to supply, encrypted at rest.

**From meetings the delegate joins:**

- The transcript of what was said while the delegate was present.
- Speaker labels and participant names as the meeting platform exposes them.
- Decisions the delegate made, what it said, and any approvals you gave.
- Action items extracted from the conversation.
- The consent record: what was announced, what was said in reply, the outcome.

**We do not store meeting audio or video.** Audio is streamed to the
speech-to-text provider, converted to text, and discarded.

## 3. Consent mode and what it changes

If the meeting's consent mode is *all-party*, nothing from the meeting is
retained until consent is received: no transcript, no memory, no summary, no
action items. If consent is refused, the delegate announces that it will not
transcribe and leaves; only the consent record itself is kept, because that
record is the evidence that we did not retain anything.

## 4. Where your data goes

Content is sent to these processors, and only as far as serving your request
requires:

| Processor | What it receives | Why |
|---|---|---|
| Google (Gemini) or Anthropic (Claude) | Transcript excerpts, your dossier and document extracts | Deciding what to say, composing replies, writing the summary |
| Deepgram | Meeting audio stream | Speech to text |
| Cartesia | The text your delegate is about to say | Text to speech |
| Telegram | Approval requests and summaries | Your notification channel |
| Our hosting and database provider | Everything above, at rest | Running the service |

If you supply your own API keys, your content goes to those providers under
your own agreement with them, and our quota does not apply.

We do not sell personal data, we do not use it for advertising, and we do not
train models on it.

## 5. How long we keep it

| Data | Retention |
|---|---|
| Account and configuration | Until you delete your account |
| Documents and their embeddings | Until you delete them, or the account |
| Meeting transcripts and summaries | 90 days by default, then deleted |
| Consent records | 24 months (they are the evidence of lawful basis) |
| Usage metering | 24 months, aggregated |
| Security and audit logs | 12 months |

You can delete any meeting brief, any document, or your whole account at any
time from the console.

## 6. Legal basis (UK/EU users)

Contract, for running the service you signed up for. Legitimate interests, for
security, abuse prevention and metering. Consent, for processing meeting
participants' speech - which the dispatching user is responsible for obtaining,
and which the all-party consent mode exists to help collect and evidence.

## 7. Your rights

Access, correction, deletion, portability, restriction, objection, and the
right to complain to your data protection authority. Most of these are
self-service in the console; for anything else, write to the address below and
we will respond within 30 days.

**If you were a participant in a meeting a DELEG8 delegate joined** and want to
know what was retained or have it erased, write to us. We will identify the
relevant records and refer you to the user who dispatched the delegate, who is
the controller for that meeting, and we will act on their instruction or on a
valid direct request.

## 8. Security

Passwords are hashed with bcrypt. API keys you supply are encrypted at rest.
Every query is scoped to one account, and each user's documents live in their
own isolated vector index rather than a shared one. Meeting workers run one
container per meeting, so one meeting cannot see another's memory.

This is a beta. We have not completed a third-party security audit and do not
claim SOC 2 or ISO 27001 compliance.

## 9. Children

DELEG8 is not for anyone under 18 and we do not knowingly process their data.

## 10. International transfers

Our processors operate in the United States and the European Union. Transfers
rely on Standard Contractual Clauses where required.

## 11. Changes

Material changes are announced in the console and require acceptance before you
continue.

## 12. Contact

**privacy@deleg8.local** (replace with your real contact address before going
live).
