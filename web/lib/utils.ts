import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

const dateTime = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});
const timeOnly = new Intl.DateTimeFormat(undefined, {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});
const dateOnly = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" });

export function formatDateTime(value: string | Date | null | undefined): string {
  if (!value) return "—";
  const date = typeof value === "string" ? new Date(value) : value;
  return Number.isNaN(date.getTime()) ? "—" : dateTime.format(date);
}

export function formatTime(value: string | Date | null | undefined): string {
  if (!value) return "";
  const date = typeof value === "string" ? new Date(value) : value;
  return Number.isNaN(date.getTime()) ? "" : timeOnly.format(date);
}

export function formatDate(value: string | Date | null | undefined): string {
  if (!value) return "—";
  const date = typeof value === "string" ? new Date(value) : value;
  return Number.isNaN(date.getTime()) ? "—" : dateOnly.format(date);
}

export function timeAgo(value: string | Date | null | undefined, now: number = Date.now()): string {
  if (!value) return "";
  const date = typeof value === "string" ? new Date(value) : value;
  const seconds = Math.round((now - date.getTime()) / 1000);
  if (!Number.isFinite(seconds)) return "";
  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  return `${days} d ago`;
}

export function formatDuration(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(sec).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

export function formatMinutes(minutes: number): string {
  if (minutes < 1) return "< 1 min";
  if (minutes < 60) return `${Math.round(minutes)} min`;
  const h = Math.floor(minutes / 60);
  const m = Math.round(minutes % 60);
  return m ? `${h} h ${m} min` : `${h} h`;
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function humanTopic(topic: string | null | undefined): string {
  if (!topic) return "—";
  return topic.replace(/_/g, " ");
}

export function slugify(text: string): string {
  const slug = text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .replace(/_{2,}/g, "_");
  const safe = slug.replace(/^[^a-z]+/, "");
  return safe || "rule";
}

export function truncate(text: string, max = 120): string {
  return text.length <= max ? text : `${text.slice(0, max - 1).trimEnd()}…`;
}

// Mirrors src/gateway/bot_base.py's detect_platform / meeting_identifier -
// keep the two in sync if a platform's URL shape changes.
export type MeetingPlatform = "google_meet" | "zoom" | "teams";

export const PLATFORM_LABELS: Record<MeetingPlatform, string> = {
  google_meet: "Google Meet",
  zoom: "Zoom",
  teams: "Microsoft Teams",
};

const PLATFORM_URL_PATTERNS: [MeetingPlatform, RegExp][] = [
  ["google_meet", /^https:\/\/meet\.google\.com\//i],
  ["zoom", /^https:\/\/[\w.-]*\.?zoom\.(?:us|com)\//i],
  ["teams", /^https:\/\/(?:teams\.microsoft\.com|teams\.live\.com|[\w.-]+\.teams\.microsoft\.com)\//i],
];

const MEETING_ID_PATTERNS: [MeetingPlatform, RegExp][] = [
  ["google_meet", /^https:\/\/meet\.google\.com\/([a-z]{3}-[a-z]{4}-[a-z]{3})(?:[/?#].*)?$/i],
  ["zoom", /^https:\/\/[\w.-]*\.?zoom\.(?:us|com)\/(?:j|wc(?:\/join)?)\/(\d{9,11})/i],
  ["teams", /^https:\/\/[\w.-]*teams\.(?:microsoft|live)\.com\/(?:l\/meetup-join\/|meet\/)([^/?#]+)/i],
];

export function detectMeetingPlatform(url: string): MeetingPlatform | null {
  const trimmed = url.trim();
  for (const [platform, re] of PLATFORM_URL_PATTERNS) {
    if (re.test(trimmed)) return platform;
  }
  return null;
}

/** A Meet code, Zoom meeting number, or Teams meeting token - whichever this link's platform uses. */
export function meetingCode(url: string): string | null {
  const trimmed = url.trim();
  for (const [, re] of MEETING_ID_PATTERNS) {
    const match = re.exec(trimmed);
    if (match) return match[1];
  }
  return null;
}

/** Matches src/gateway/session.py's _ID_PREFIX_BY_PLATFORM - "meet" kept for Google
 * Meet specifically since it predates multi-platform support and existing meeting
 * ids already use it. */
const ID_PREFIX_BY_PLATFORM: Record<MeetingPlatform, string> = {
  google_meet: "meet",
  zoom: "zoom",
  teams: "teams",
};

export function meetingIdPrefix(platform: MeetingPlatform): string {
  return ID_PREFIX_BY_PLATFORM[platform];
}
