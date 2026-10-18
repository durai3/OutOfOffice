import { API_BASE } from "@/lib/config";
import { clearTokens, getAccessToken, refreshTokens } from "@/lib/auth-store";
import type {
  Account,
  CalendarAccount,
  CalendarEvent,
  CalendarProviderInfo,
  ChannelSummary,
  ChunkPage,
  CommitmentsResponse,
  CommitmentTrackerRow,
  ConnectorOption,
  ConnectorSummary,
  ConsentModeInfo,
  ConsentRecord,
  CredentialSummary,
  DashboardResponse,
  DegradationReport,
  DelegateStatus,
  DossierResponse,
  HealthAnalyticsResponse,
  HealthResponse,
  IngestReport,
  InvitePreview,
  JoinRequest,
  KnowledgeOverview,
  LegalDocument,
  LegalStatus,
  LegalSummary,
  MeetingBrief,
  MeetingListResponse,
  MeetingSnapshot,
  MeetingUsage,
  OAuthStartResponse,
  OnboardingState,
  OrganizationInvite,
  OrganizationMember,
  OrganizationSummary,
  PolicyTemplate,
  PostMeetingReport,
  RecentMeeting,
  RemoveDocumentResponse,
  RetrievalResult,
  RoiResponse,
  SearchResponse,
  SettingsResponse,
  SignupRequest,
  TelegramLink,
  TokenResponse,
  UploadReport,
  UsageEvent,
  UsageSummary,
  UserProfile,
} from "@/lib/types";

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, message: string, detail: unknown = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

interface ValidationItem {
  loc?: unknown[];
  msg?: string;
}

function describeDetail(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        const entry = item as ValidationItem;
        const loc = Array.isArray(entry.loc) ? entry.loc.filter((part) => part !== "body").join(".") : "";
        const msg = (entry.msg ?? "invalid value").replace(/^Value error, /, "");
        return loc ? `${loc}: ${msg}` : msg;
      })
      .join("; ");
  }
  if (detail && typeof detail === "object" && "detail" in detail) {
    return describeDetail((detail as { detail: unknown }).detail);
  }
  return "";
}

/** Endpoints that must not carry (or retry with) a token: the ones that mint them. */
const PUBLIC_PATHS = [
  "/api/v1/auth/login",
  "/api/v1/auth/signup",
  "/api/v1/auth/refresh",
  "/api/v1/auth/oauth",
  "/api/v1/health",
  "/api/v1/legal/documents",
  // Note: the invite preview endpoint (GET .../invites/by-token/{token}) is
  // not session-authed either, but it is deliberately NOT listed here - this
  // array is matched by prefix, and the accept endpoint
  // (POST .../invites/by-token/{token}/accept, which DOES need the bearer
  // token) shares that same prefix. The preview route ignores auth entirely,
  // so sending a token when one happens to exist is harmless either way.
];

async function send(path: string, init: RequestInit, token: string | null): Promise<Response> {
  const isForm = init.body instanceof FormData;
  const headers = new Headers(init.headers ?? {});
  if (!isForm && init.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  headers.set("Accept", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return fetch(`${API_BASE}${path}`, { ...init, headers, cache: "no-store" });
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const isPublic = PUBLIC_PATHS.some((p) => path.startsWith(p));
  let response: Response;
  try {
    response = await send(path, init, isPublic ? null : getAccessToken());
  } catch (error) {
    throw new ApiError(0, `Cannot reach the DELEG8 API at ${API_BASE}. Is the FastAPI server running?`, error);
  }

  // One silent refresh on a 401, then one retry. A body that has already been
  // read cannot be replayed, so a FormData upload is not retried - the caller
  // sees the 401 and the auth context signs them out, which is the honest
  // outcome for a file that was never accepted.
  if (response.status === 401 && !isPublic && !(init.body instanceof FormData)) {
    const refreshed = await refreshTokens();
    if (refreshed) {
      try {
        response = await send(path, init, refreshed.access);
      } catch (error) {
        throw new ApiError(0, `Cannot reach the DELEG8 API at ${API_BASE}.`, error);
      }
    } else {
      clearTokens();
    }
  }

  if (!response.ok) {
    let detail: unknown = null;
    const text = await response.text().catch(() => "");
    try {
      detail = text ? JSON.parse(text) : null;
    } catch {
      detail = text;
    }
    const message = describeDetail(detail && typeof detail === "object" && "detail" in detail ? (detail as { detail: unknown }).detail : detail);
    throw new ApiError(response.status, message || `${response.status} ${response.statusText}`, detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function json(body: unknown): string {
  return JSON.stringify(body);
}

function encodePath(source: string): string {
  return source.split("/").map(encodeURIComponent).join("/");
}

export const api = {
  health: () => request<HealthResponse>("/api/v1/health"),
  dashboard: () => request<DashboardResponse>("/api/v1/dashboard"),
  delegate: () => request<DelegateStatus>("/api/v1/delegate"),
  delegates: () => request<DelegateStatus[]>("/api/v1/delegates"),
  join: (body: JoinRequest) => request<DelegateStatus>("/api/v1/meetings/join", { method: "POST", body: json(body) }),
  abort: (reason?: string, meetingId?: string) =>
    request<DelegateStatus>("/api/v1/meetings/abort", {
      method: "POST",
      body: json({ ...(reason ? { reason } : {}), ...(meetingId ? { meeting_id: meetingId } : {}) }),
    }),
  recentMeetings: (limit = 25) => request<RecentMeeting[]>(`/api/v1/meetings/recent?limit=${limit}`),

  // -- accounts -------------------------------------------------------- //
  signup: (body: SignupRequest) => request<TokenResponse>("/api/v1/auth/signup", { method: "POST", body: json(body) }),
  login: (email: string, password: string) =>
    request<TokenResponse>("/api/v1/auth/login", { method: "POST", body: json({ email, password }) }),
  logout: (refreshToken: string) =>
    request<{ detail: string }>("/api/v1/auth/logout", { method: "POST", body: json({ refresh_token: refreshToken }) }),
  me: () => request<Account>("/api/v1/auth/me"),
  updateAccount: (body: { display_name?: string; principal_name?: string; aliases?: string[] }) =>
    request<Account>("/api/v1/auth/me", { method: "PATCH", body: json(body) }),
  changePassword: (current_password: string, new_password: string) =>
    request<{ detail: string }>("/api/v1/auth/password", { method: "POST", body: json({ current_password, new_password }) }),
  deleteAccount: () => request<{ detail: string }>("/api/v1/auth/me", { method: "DELETE" }),
  startSsoLogin: (provider: "google" | "microsoft", origin: string, acceptTerms = false) =>
    request<OAuthStartResponse>(
      `/api/v1/auth/oauth/${provider}/start?origin=${encodeURIComponent(origin)}&accept_terms=${acceptTerms}`,
    ),

  // -- legal ----------------------------------------------------------- //
  legalDocuments: () => request<LegalSummary[]>("/api/v1/legal/documents"),
  legalDocument: (key: string) => request<LegalDocument>(`/api/v1/legal/documents/${encodeURIComponent(key)}`),
  legalStatus: () => request<LegalStatus>("/api/v1/legal/status"),
  acceptLegal: (documents?: string[]) =>
    request<LegalStatus>("/api/v1/legal/accept", { method: "POST", body: json({ documents: documents ?? null }) }),

  // -- onboarding ------------------------------------------------------ //
  onboarding: () => request<OnboardingState>("/api/v1/onboarding"),
  policyTemplates: () => request<PolicyTemplate[]>("/api/v1/onboarding/templates"),
  choosePolicy: (template: string | null) =>
    request<OnboardingState>("/api/v1/onboarding/policy", { method: "POST", body: json({ template }) }),
  telegramCode: () => request<TelegramLink>("/api/v1/onboarding/telegram", { method: "POST" }),
  skipStep: (step: string) => request<OnboardingState>("/api/v1/onboarding/skip", { method: "POST", body: json({ step }) }),
  completeOnboarding: () => request<OnboardingState>("/api/v1/onboarding/complete", { method: "POST" }),

  // -- settings -------------------------------------------------------- //
  settings: () => request<SettingsResponse>("/api/v1/settings"),
  savePreferences: (body: { consent_mode?: string; escalation_timeout_seconds?: number }) =>
    request<SettingsResponse>("/api/v1/settings/preferences", { method: "PUT", body: json(body) }),
  storeKey: (provider: string, key: string, verify = true) =>
    request<CredentialSummary[]>(`/api/v1/settings/credentials/${provider}`, { method: "PUT", body: json({ key, verify }) }),
  deleteKey: (provider: string) =>
    request<CredentialSummary[]>(`/api/v1/settings/credentials/${provider}`, { method: "DELETE" }),
  settingsTelegramCode: () => request<ChannelSummary>("/api/v1/settings/telegram/code", { method: "POST" }),
  useOwnBot: (bot_token: string, chat_id: string) =>
    request<ChannelSummary>("/api/v1/settings/telegram/bot", { method: "POST", body: json({ bot_token, chat_id }) }),
  unlinkTelegram: () => request<ChannelSummary>("/api/v1/settings/telegram", { method: "DELETE" }),

  // -- usage ----------------------------------------------------------- //
  usage: () => request<UsageSummary>("/api/v1/usage"),
  usageEvents: (limit = 50) => request<UsageEvent[]>(`/api/v1/usage/events?limit=${limit}`),
  usageByMeeting: (limit = 20) => request<MeetingUsage[]>(`/api/v1/usage/meetings?limit=${limit}`),

  // -- consent --------------------------------------------------------- //
  consentModes: () => request<ConsentModeInfo[]>("/api/v1/consent/modes"),
  consentRecords: (limit = 100) => request<ConsentRecord[]>(`/api/v1/consent/records?limit=${limit}`),
  resolveConsent: (meetingId: string, granted: boolean) =>
    request<DelegateStatus>(`/api/v1/meetings/${encodeURIComponent(meetingId)}/consent`, {
      method: "POST",
      body: json({ granted }),
    }),

  profile: () => request<UserProfile>("/api/v1/profile"),
  saveProfile: (profile: UserProfile) => request<UserProfile>("/api/v1/profile", { method: "PUT", body: json(profile) }),

  knowledge: () => request<KnowledgeOverview>("/api/v1/knowledge"),
  knowledgeChunks: (source: string, limit = 200, offset = 0) =>
    request<ChunkPage>(`/api/v1/knowledge/chunks?source=${encodeURIComponent(source)}&limit=${limit}&offset=${offset}`),
  upload: (files: File[]) => {
    const form = new FormData();
    for (const file of files) form.append("files", file, file.name);
    return request<UploadReport>("/api/v1/knowledge/upload", { method: "POST", body: form });
  },
  removeDocument: (source: string) =>
    request<RemoveDocumentResponse>(`/api/v1/knowledge/documents/${encodePath(source)}`, { method: "DELETE" }),
  reindex: (force = false) => request<IngestReport>("/api/v1/knowledge/ingest", { method: "POST", body: json({ force }) }),
  query: (query: string, topK?: number, minSimilarity?: number) =>
    request<RetrievalResult>("/api/v1/knowledge/query", {
      method: "POST",
      body: json({ query, top_k: topK ?? null, min_similarity: minSimilarity ?? null }),
    }),

  dossier: () => request<DossierResponse>("/api/v1/dossier"),
  saveDossier: (markdown: string) => request<DossierResponse>("/api/v1/dossier", { method: "PUT", body: json({ markdown }) }),
  rebuildDossier: (force = true) =>
    request<DossierResponse>(`/api/v1/dossier/rebuild?force=${force}`, { method: "POST" }),
  recheckLlm: () => request<DegradationReport>("/api/v1/health/recheck", { method: "POST" }),
  commitments: () => request<CommitmentsResponse>("/api/v1/commitments"),

  history: (limit?: number) => request<MeetingBrief[]>(`/api/v1/history${limit ? `?limit=${limit}` : ""}`),
  brief: (briefId: string) => request<PostMeetingReport>(`/api/v1/history/${encodeURIComponent(briefId)}`),

  meetings: () => request<MeetingListResponse>("/api/v1/meetings"),
  meeting: (meetingId: string) => request<MeetingSnapshot>(`/api/v1/meetings/${encodeURIComponent(meetingId)}`),
  conclude: (meetingId: string) =>
    request<PostMeetingReport>(`/api/v1/meetings/${encodeURIComponent(meetingId)}/conclude`, { method: "POST" }),

  // -- integrations ------------------------------------------------------ //
  integrations: () => request<ConnectorSummary[]>("/api/v1/integrations"),
  authorizeIntegration: (connectorType: string, credential: string | null, config: Record<string, unknown> = {}) =>
    request<ConnectorSummary>(`/api/v1/integrations/${encodeURIComponent(connectorType)}/authorize`, {
      method: "POST",
      body: json({ credential, config }),
    }),
  startOAuth: (connectorType: string, origin: string) =>
    request<OAuthStartResponse>(
      `/api/v1/integrations/${encodeURIComponent(connectorType)}/oauth/start?origin=${encodeURIComponent(origin)}`,
    ),
  connectorOptions: (connectorType: string) =>
    request<ConnectorOption[]>(`/api/v1/integrations/${encodeURIComponent(connectorType)}/options`),
  patchIntegration: (connectorType: string, body: { is_enabled?: boolean; config?: Record<string, unknown> }) =>
    request<ConnectorSummary>(`/api/v1/integrations/${encodeURIComponent(connectorType)}`, {
      method: "PATCH",
      body: json(body),
    }),
  disconnectIntegration: (connectorType: string) =>
    request<void>(`/api/v1/integrations/${encodeURIComponent(connectorType)}`, { method: "DELETE" }),

  // -- search -------------------------------------------------------------- //
  search: (q: string, type?: "meeting" | "commitment") =>
    request<SearchResponse>(`/api/v1/search?q=${encodeURIComponent(q)}${type ? `&type=${type}` : ""}`),

  // -- commitments tracker -------------------------------------------------- //
  commitmentsTracker: (status: string = "open", sort: string = "due_date") =>
    request<CommitmentTrackerRow[]>(`/api/v1/commitments/tracker?status=${encodeURIComponent(status)}&sort=${encodeURIComponent(sort)}`),
  markCommitmentDone: (id: string) =>
    request<CommitmentTrackerRow>(`/api/v1/commitments/tracker/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: json({ action: "done" }),
    }),
  dropCommitment: (id: string) =>
    request<CommitmentTrackerRow>(`/api/v1/commitments/tracker/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: json({ action: "dropped" }),
    }),
  snoozeCommitment: (id: string, days: number = 1) =>
    request<CommitmentTrackerRow>(`/api/v1/commitments/tracker/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: json({ action: "snooze", snooze_days: days }),
    }),

  // -- analytics ------------------------------------------------------------ //
  analyticsHealth: (weeks: number = 4) => request<HealthAnalyticsResponse>(`/api/v1/analytics/health?weeks=${weeks}`),
  analyticsRoi: (days: number = 30) => request<RoiResponse>(`/api/v1/analytics/roi?days=${days}`),

  // -- calendar --------------------------------------------------------- //
  calendarProviders: () => request<CalendarProviderInfo[]>("/api/v1/calendar/providers"),
  calendarAccounts: () => request<CalendarAccount[]>("/api/v1/calendar/accounts"),
  startCalendarOAuth: (provider: string, origin: string) =>
    request<OAuthStartResponse>(
      `/api/v1/calendar/accounts/${encodeURIComponent(provider)}/oauth/start?origin=${encodeURIComponent(origin)}`,
    ),
  patchCalendarAccount: (accountId: string, is_enabled: boolean) =>
    request<CalendarAccount>(`/api/v1/calendar/accounts/${encodeURIComponent(accountId)}`, {
      method: "PATCH",
      body: json({ is_enabled }),
    }),
  syncCalendarAccount: (accountId: string) =>
    request<CalendarAccount>(`/api/v1/calendar/accounts/${encodeURIComponent(accountId)}/sync`, { method: "POST" }),
  disconnectCalendarAccount: (accountId: string) =>
    request<void>(`/api/v1/calendar/accounts/${encodeURIComponent(accountId)}`, { method: "DELETE" }),
  calendarEvents: () => request<CalendarEvent[]>("/api/v1/calendar/events"),
  setEventDeleg8: (eventId: string, deleg8_requested: boolean) =>
    request<CalendarEvent>(`/api/v1/calendar/events/${encodeURIComponent(eventId)}`, {
      method: "PATCH",
      body: json({ deleg8_requested }),
    }),

  // -- organizations ----------------------------------------------------- //
  createOrganization: (name: string) =>
    request<OrganizationSummary>("/api/v1/organizations", { method: "POST", body: json({ name }) }),
  myOrganization: () => request<OrganizationSummary>("/api/v1/organizations/me"),
  organizationMembers: () => request<OrganizationMember[]>("/api/v1/organizations/members"),
  inviteMember: (email: string, role: string) =>
    request<OrganizationInvite>("/api/v1/organizations/invites", { method: "POST", body: json({ email, role }) }),
  listInvites: () => request<OrganizationInvite[]>("/api/v1/organizations/invites"),
  resendInvite: (id: string) =>
    request<OrganizationInvite>(`/api/v1/organizations/invites/${encodeURIComponent(id)}/resend`, { method: "POST" }),
  revokeInvite: (id: string) =>
    request<{ detail: string }>(`/api/v1/organizations/invites/${encodeURIComponent(id)}`, { method: "DELETE" }),
  setMemberRole: (userId: string, role: string) =>
    request<OrganizationMember>(`/api/v1/organizations/members/${encodeURIComponent(userId)}`, {
      method: "PATCH",
      body: json({ role }),
    }),
  removeMember: (userId: string) =>
    request<{ detail: string }>(`/api/v1/organizations/members/${encodeURIComponent(userId)}`, { method: "DELETE" }),
  leaveOrganization: () => request<{ detail: string }>("/api/v1/organizations/leave", { method: "POST" }),
  previewInvite: (token: string) =>
    request<InvitePreview>(`/api/v1/organizations/invites/by-token/${encodeURIComponent(token)}`),
  acceptInvite: (token: string) =>
    request<OrganizationMember>(`/api/v1/organizations/invites/by-token/${encodeURIComponent(token)}/accept`, {
      method: "POST",
    }),
};

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return String(error);
}
