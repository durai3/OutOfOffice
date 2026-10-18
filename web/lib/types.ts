/* Mirrors of the FastAPI Pydantic contracts. Keep in step with src/. */

export type PolicyTier = "GREEN" | "YELLOW" | "RED";
export type DecisionState = "AUTO_REPLY" | "SILENT" | "ESCALATE";
export type EscalationOutcome = "APPROVED" | "REJECTED" | "DECLINED_POLITELY" | "TIMEOUT" | "UNDELIVERED";
export type DelegateActivity =
  | "IDLE"
  | "CONNECTING"
  | "LISTENING"
  | "THINKING"
  | "AWAITING_APPROVAL"
  | "AWAITING_CONSENT"
  | "SPEAKING"
  | "LEAVING";
export type MeetPhase =
  | "IDLE"
  | "STARTING"
  | "LOGGING_IN"
  | "JOINING"
  | "LOBBY"
  | "IN_CALL"
  | "RECONNECTING"
  | "REMOVED"
  | "ENDED"
  | "LEFT"
  | "ERROR";

export interface PolicyRule {
  topic: string;
  tier: PolicyTier;
  description: string;
  triggers: string[];
}

export interface KnowledgeEntry {
  topic: string;
  subject: string;
  keywords: string[];
  answer: string;
}

export interface UserProfile {
  user_id: string;
  display_name: string;
  aliases: string[];
  policy: PolicyRule[];
  knowledge_base: KnowledgeEntry[];
}

export interface Grounding {
  source: "profile" | "documents";
  subject: string;
  similarity: number | null;
  citations: string[];
  detail: string | null;
}

export interface TraceStep {
  stage: string;
  outcome: string;
}

export interface PolicyDecision {
  state: DecisionState;
  reason: string;
  topic: string | null;
  tier: PolicyTier | null;
  matched_triggers: string[];
  suggested_response: string | null;
  grounding: Grounding | null;
  trace: TraceStep[];
  evaluated_at: string;
  requires_human: boolean;
}

export interface EscalationResult {
  request_id: string;
  outcome: EscalationOutcome;
  delivered: boolean;
  responded_by: string | null;
  response_seconds: number;
  message_id: number | null;
  detail: string | null;
  resolved_at: string;
  human_decided: boolean;
}

export interface SpeakerTurn {
  index: number;
  speaker: string;
  text: string;
  timestamp: string;
  decision: DecisionState | null;
  topic: string | null;
  tier: PolicyTier | null;
  is_delegate: boolean;
}

export interface AgreedDecision {
  text: string;
  proposed_by: string;
  origin: "utterance" | "human_approved" | "human_rejected";
  turn_index: number;
  timestamp: string;
}

export interface ActionItem {
  description: string;
  owner: string;
  owner_inferred: boolean;
  due_text: string | null;
  due_date: string | null;
  turn_index: number;
  timestamp: string;
}

export interface OpenQuestion {
  text: string;
  asked_by: string;
  topic: string | null;
  tier: PolicyTier | null;
  outcome: string;
  turn_index: number;
  timestamp: string;
}

export interface MeetingStats {
  utterances: number;
  auto_replies: number;
  escalations: number;
  silent: number;
  human_decisions: number;
}

export interface TurnUpdate {
  turn_index: number;
  active_topic: string | null;
  topic_changed: boolean;
  new_decisions: AgreedDecision[];
  new_actions: ActionItem[];
  new_questions: OpenQuestion[];
}

export interface MeetingSnapshot {
  meeting_id: string;
  user_id: string;
  started_at: string;
  concluded_at: string | null;
  participants: string[];
  active_topic: string | null;
  speaker_turns: SpeakerTurn[];
  transcript: SpeakerTurn[];
  agreed_decisions: AgreedDecision[];
  pending_actions: ActionItem[];
  open_questions: OpenQuestion[];
  stats: MeetingStats;
}

export interface SessionCounters {
  utterances: number;
  auto_replies: number;
  escalations: number;
  silent: number;
  interruptions: number;
  brain_avg_ms: number | null;
  first_audio_avg_ms: number | null;
}

export interface DelegateStatus {
  active: boolean;
  phase: MeetPhase | string;
  activity: DelegateActivity;
  meeting_id: string | null;
  meeting_row_id: string | null;
  meeting_code: string | null;
  platform: "google_meet" | "zoom" | "teams" | null;
  /** Where this meeting actually runs: one container per meeting, or in the API server. */
  runtime: "container" | "inprocess" | null;
  container: string | null;
  join_url: string | null;
  speaker_label: string | null;
  principal: string | null;
  started_at: string | null;
  joined_at: string | null;
  finished_at: string | null;
  detail: string | null;
  stop_reason: string | null;
  exit_code: number | null;
  error: string | null;
  active_topic: string | null;
  current_speaker: string | null;
  last_utterance: string | null;
  speaking_text: string | null;
  pending_escalations: number;
  consent_mode: ConsentMode;
  consent_state: ConsentState;
  /** False while consent is blocking transcript retention. */
  retaining: boolean;
  counters: SessionCounters;
  live_subscribers: number;
  updated_at: string;
}

export interface JoinRequest {
  url: string;
  meeting_id?: string | null;
  speaker_label?: string;
  half_duplex?: boolean;
  briefing?: boolean;
  max_minutes?: number | null;
  /** Override the account default for this meeting only. */
  consent_mode?: ConsentMode | null;
}

export interface ProfileSummary {
  user_id: string;
  display_name: string;
  aliases: string[];
  rules_by_tier: Record<PolicyTier, number>;
  total_rules: number;
  knowledge_entries: number;
}

export interface ActionItemSummary {
  description: string;
  owner: string;
  due: string;
}

export interface ExecutiveBriefing {
  executive_summary: string;
  decisions_made: string[];
  action_items: ActionItemSummary[];
  unresolved_questions: string[];
}

export interface DeliveryReceipt {
  delivered: boolean;
  channel: string;
  message_id: number | null;
  detail: string | null;
  sent_at: string;
}

export interface PostMeetingReport {
  meeting_id: string;
  concluded_at: string;
  briefing: ExecutiveBriefing;
  summariser: string;
  fallback_reason: string | null;
  text: string;
  receipt: DeliveryReceipt;
  snapshot: MeetingSnapshot;
}

export interface MeetingBrief {
  brief_id: string;
  meeting_id: string;
  user_id: string;
  started_at: string;
  concluded_at: string;
  duration_minutes: number;
  participants: string[];
  executive_summary: string;
  decisions_made: string[];
  action_items: ActionItemSummary[];
  unresolved_questions: string[];
  summariser: string;
  fallback_reason: string | null;
  delivered: boolean;
  delivery_channel: string;
  stats: MeetingStats;
}

export interface DashboardResponse {
  version: string;
  generated_at: string;
  delegate: DelegateStatus;
  /** Every meeting this account currently has running. */
  delegates: DelegateStatus[];
  profile: ProfileSummary;
  meetings_attended: number;
  pending_action_items: number;
  live_meetings: string[];
  knowledge_available: boolean;
  knowledge_index_size: number | null;
  escalation_bridge: string;
  knowledge: string;
  summariser: string;
  recent_briefs: MeetingBrief[];
  plan: string;
  usage_fractions: Record<string, number>;
  quota_blocked: boolean;
  quota_reason: string | null;
  consent_mode: ConsentMode;
  onboarding_complete: boolean;
  healthy: boolean;
  banner: string | null;
}

export interface Degradation {
  component: string;
  summary: string;
  detail: string | null;
  since: string;
  count: number;
}

/**
 * Deployment health. Unauthenticated, so deliberately says nothing about any
 * one tenant - per-account health is on the dashboard.
 */
export interface HealthResponse {
  status: string;
  version: string;
  database: string;
  escalation: string;
  /** How meetings are run, and whether that runtime is usable right now. */
  dispatch: string;
  workspaces: string;
  llm: string;
  /** False when any component is running in a fallback mode. */
  healthy: boolean;
  /** One line summarising every degradation, or null when everything is fine. */
  banner: string | null;
  degradations: Degradation[];
  generated_at: string;
}

export interface DegradationReport {
  healthy: boolean;
  banner: string | null;
  degradations: Degradation[];
  llm: string;
  planner_ready: boolean;
}

/** What the delegate knows about its principal: the product's trust surface. */
export interface DossierResponse {
  user_id: string;
  markdown: string;
  describe: string;
  hand_edited: boolean;
  built_at: string | null;
  built_by: string | null;
  sources: string[];
  sections: string[];
}

export interface Commitment {
  id: string;
  description: string;
  owner: string;
  requested_by: string;
  due_iso: string;
  due_text: string;
  status: "probable" | "confirmed" | "done" | "dropped";
  source: "planner" | "rules" | "both";
  acknowledged: boolean;
  first_turn: number;
  last_turn: number;
  created_at: string;
  updated_at: string;
  notified_at: string | null;
}

export interface CommitmentsResponse {
  total: number;
  due_soon: number;
  by_meeting: Record<string, Commitment[]>;
}

export interface IndexedDocument {
  source: string;
  title: string;
  chunks: number;
  characters: number;
  pages: number | null;
  sha256: string | null;
  on_disk: boolean;
  size_bytes: number | null;
  modified_at: string | null;
}

export interface UnindexedFile {
  source: string;
  size_bytes: number;
  modified_at: string;
}

export interface KnowledgeOverview {
  available: boolean;
  detail: string | null;
  collection: string;
  chroma_dir: string;
  documents_dir: string;
  embedding_model: string;
  min_similarity: number;
  top_k: number;
  chunk_size: number;
  chunk_overlap: number;
  index_size: number;
  documents: IndexedDocument[];
  unindexed_files: UnindexedFile[];
  supported_suffixes: string[];
  upload_limit_bytes: number;
}

export interface IndexedChunk {
  chunk_id: string;
  source: string;
  title: string;
  section: string;
  page: number | null;
  chunk_index: number;
  characters: number;
  text: string;
}

export interface ChunkPage {
  source: string;
  total: number;
  offset: number;
  limit: number;
  chunks: IndexedChunk[];
}

export interface FileIngestResult {
  source: string;
  status: "ingested" | "skipped" | "failed" | string;
  chunks: number;
  characters: number;
  detail: string | null;
}

export interface UploadReport {
  documents_dir: string;
  files: FileIngestResult[];
  index_size: number;
}

export interface IngestReport {
  documents_dir: string;
  files: FileIngestResult[];
  index_size: number;
  elapsed_seconds: number;
}

export interface RemoveDocumentResponse {
  source: string;
  removed_chunks: number;
  file_deleted: boolean;
  index_size: number;
}

export interface RetrievedChunk {
  chunk_id: string;
  text: string;
  source: string;
  title: string;
  section: string;
  page: number | null;
  similarity: number;
}

export interface RetrievalResult {
  query: string;
  status: "GROUNDED" | "UNKNOWN_CONTEXT";
  chunks: RetrievedChunk[];
  best_similarity: number | null;
  threshold: number;
  index_size: number;
  elapsed_ms: number;
  grounded: boolean;
}

export interface MeetingListResponse {
  active: string[];
}

/* Live feed frames -------------------------------------------------------- */

export interface LiveEvent<T = Record<string, unknown>> {
  seq: number;
  type: string;
  at: string;
  payload: T;
}

export interface HelloPayload {
  status: DelegateStatus;
  snapshot: MeetingSnapshot | null;
  backlog: LiveEvent[];
  server_version: string;
}

/* ------------------------------------------------------------------ */
/* Multi-tenancy: accounts, onboarding, settings, usage, consent.      */
/* ------------------------------------------------------------------ */

export interface PrincipalSummary {
  id: string;
  slug: string;
  display_name: string;
  aliases: string[];
}

export type OrganizationRole = "owner" | "admin" | "member";

export interface OrganizationSummary {
  id: string;
  name: string;
  role: OrganizationRole;
  plan: string;
  seats_used: number;
  seats_limit: number;
}

export interface OrganizationMember {
  user_id: string;
  email: string;
  display_name: string;
  role: OrganizationRole;
  joined_at: string;
  meetings_this_month?: number | null;
  llm_tokens?: number | null;
  estimated_cost_usd?: number | null;
}

export interface OrganizationInvite {
  id: string;
  email: string;
  role: OrganizationRole;
  invite_url?: string | null;
  expires_at: string;
  accepted_at: string | null;
  revoked_at: string | null;
}

export interface InvitePreview {
  org_name: string;
  email: string;
  role: OrganizationRole;
  valid: boolean;
}

export interface Account {
  id: string;
  email: string;
  display_name: string;
  role: "user" | "admin";
  status: "active" | "suspended";
  created_at: string;
  last_login_at: string | null;
  principal: PrincipalSummary | null;
  onboarding: Record<string, boolean>;
  onboarding_complete: boolean;
  outstanding_legal: string[];
  settings: Record<string, unknown>;
  organization: OrganizationSummary | null;
}

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_at: string;
  account: Account;
}

export interface SignupRequest {
  email: string;
  password: string;
  display_name: string;
  accept_terms: boolean;
  policy_template: string;
  invite_token?: string | null;
}

export interface LegalSummary {
  key: string;
  title: string;
  version: string;
  effective_date: string;
  summary: string;
  required: boolean;
}

export interface LegalDocument extends LegalSummary {
  body: string;
}

export interface LegalStatus {
  current_versions: Record<string, string>;
  accepted_versions: Record<string, string>;
  outstanding: string[];
  up_to_date: boolean;
  history: { document: string; version: string; accepted_at: string }[];
}

export interface PolicyTemplate {
  key: string;
  name: string;
  summary: string;
  rule_count: number;
  tiers: Record<PolicyTier, number>;
}

export interface OnboardingStep {
  key: string;
  title: string;
  description: string;
  complete: boolean;
  optional: boolean;
  detail: string | null;
}

export interface TelegramLink {
  code: string | null;
  expires_at: string | null;
  bot_username: string | null;
  linked: boolean;
  instructions: string;
}

export interface OnboardingState {
  complete: boolean;
  next_step: string | null;
  steps: OnboardingStep[];
  account: Account;
  telegram: TelegramLink;
}

export type ConsentMode = "notify_only" | "all_party" | "single_party";
export type ConsentState = "not_required" | "pending" | "granted" | "denied" | "timed_out" | "withdrawn";

export interface ConsentModeInfo {
  key: ConsentMode;
  name: string;
  summary: string;
}

export interface ConsentRecord {
  id: string;
  meeting_key: string;
  mode: ConsentMode;
  outcome: ConsentState;
  method: string;
  granted_by: string | null;
  evidence: string | null;
  disclosure_text: string | null;
  recorded_at: string;
}

export interface CredentialSummary {
  provider: string;
  label: string;
  help_url: string;
  configured: boolean;
  own_key: boolean;
  hint: string;
  verified_at: string | null;
  verify_error: string | null;
}

export interface ChannelSummary {
  kind: string;
  linked: boolean;
  own_bot: boolean;
  label: string | null;
  bot_username: string | null;
  link_code: string | null;
  link_code_expires_at: string | null;
  timeout_seconds: number | null;
  describe: string;
}

export interface Preferences {
  consent_mode: ConsentMode;
  escalation_timeout_seconds: number;
  plan: string;
}

export interface SettingsResponse {
  preferences: Preferences;
  consent_modes: ConsentModeInfo[];
  credentials: CredentialSummary[];
  channel: ChannelSummary;
  plan_name: string;
  plan_describe: string;
  using_own_keys: boolean;
  usage_fractions: Record<string, number>;
}

export interface AllowanceLine {
  key: string;
  label: string;
  used: number;
  allowed: number;
  unit: string;
  fraction: number;
  unlimited: boolean;
}

export interface UsageSummary {
  period_start: string;
  plan: string;
  plan_describe: string;
  using_own_keys: boolean;
  blocked: boolean;
  reason: string | null;
  near_limit: string[];
  estimated_cost_usd: number;
  lines: AllowanceLine[];
  providers: string;
}

export interface UsageEvent {
  id: string;
  service: "llm" | "stt" | "tts" | "meeting";
  provider: string;
  model: string;
  unit: string;
  input_units: number;
  output_units: number;
  units: number;
  cost_usd: number;
  own_key: boolean;
  meeting_id: string | null;
  detail: string | null;
  created_at: string;
}

export interface MeetingUsage {
  meeting_id: string;
  meeting_key: string;
  started_at: string | null;
  ended_at: string | null;
  status: string;
  units: Record<string, number>;
}

/* ------------------------------------------------------------------ */
/* Integrations, search, commitments tracker, analytics.               */
/* ------------------------------------------------------------------ */

export type ConnectorType = "slack" | "ms_teams" | "jira" | "linear" | "asana" | "google_calendar" | "email" | "notion";
export type AuthMode = "token" | "oauth";

export interface ConnectorField {
  name: string;
  label: string;
  secret: boolean;
  placeholder: string;
}

export interface ConnectorSummary {
  connector_type: ConnectorType | string;
  label: string;
  auth_mode: AuthMode;
  available: boolean;
  unavailable_reason: string | null;
  docs_url: string;
  fields: ConnectorField[];
  is_connected: boolean;
  is_enabled: boolean;
  config: Record<string, unknown>;
  last_synced_at: string | null;
  last_error: string | null;
}

export interface OAuthStartResponse {
  redirect_url: string;
}

export interface ConnectorOption {
  id: string;
  name: string;
}

/** Posted by the OAuth callback popup to window.opener; see src/api/integrations.py's _oauth_result_page. */
export interface OAuthPopupMessage {
  source: "deleg8-oauth";
  connector_type: string;
  ok: boolean;
  message?: string;
}

/** Posted by the "Continue with Google/Microsoft" login popup; see src/auth/routes.py's _login_result_page. */
export interface OAuthLoginPopupMessage {
  source: "deleg8-oauth-login";
  ok: boolean;
  message?: string;
  tokens?: TokenResponse;
}

/* ------------------------------------------------------------------ */
/* Calendar accounts and synced meetings (src/api/calendar.py).        */
/* ------------------------------------------------------------------ */

export type CalendarProviderId = "google" | "microsoft";

export interface CalendarProviderInfo {
  provider: CalendarProviderId | string;
  label: string;
  available: boolean;
  unavailable_reason: string | null;
}

export interface CalendarAccount {
  id: string;
  provider: CalendarProviderId | string;
  email: string;
  label: string;
  is_enabled: boolean;
  last_synced_at: string | null;
  last_error: string | null;
}

export interface CalendarEvent {
  id: string;
  calendar_account_id: string;
  account_email: string;
  provider: string;
  title: string;
  join_url: string;
  html_link: string;
  start_at: string;
  end_at: string | null;
  delegable: boolean;
  deleg8_requested: boolean;
  dispatched: boolean;
  meeting_id: string | null;
}

/** Posted by the calendar OAuth callback popup; see src/api/calendar.py's _oauth_result_page. */
export interface CalendarOAuthPopupMessage {
  source: "deleg8-calendar-oauth";
  ok: boolean;
  message?: string;
}

export interface SearchResult {
  type: "meeting" | "commitment";
  id: string;
  title: string;
  excerpt: string;
  meeting_key: string | null;
  occurred_at: string | null;
}

export interface SearchResponse {
  query: string;
  total: number;
  results: SearchResult[];
}

export type CommitmentStatus = "probable" | "confirmed" | "done" | "dropped";

export interface CommitmentTrackerRow {
  id: string;
  meeting_id: string;
  meeting_key: string | null;
  owner: string;
  requested_by: string;
  description: string;
  due_iso: string;
  due_text: string;
  status: CommitmentStatus;
  overdue: boolean;
}

export interface MeetingHealth {
  meeting_id: string;
  meeting_key: string;
  concluded_at: string;
  decision_clarity: number;
  participation_score: number;
  agenda_adherence: number;
  action_item_clarity: number;
  health_score: number;
  flags: string[];
}

export interface WeeklyHealthPoint {
  week_start: string;
  meetings: number;
  decision_clarity: number;
  participation_score: number;
  agenda_adherence: number;
  action_item_clarity: number;
  health_score: number;
}

export interface HealthAnalyticsResponse {
  weekly_trend: WeeklyHealthPoint[];
  meetings: MeetingHealth[];
}

export interface RoiResponse {
  days: number;
  meetings_attended: number;
  decisions_made: number;
  completion_pct: number;
  hours_saved: number;
  estimated_value_usd: number;
}

export interface RecentMeeting {
  id: string;
  meeting_key: string;
  platform: string;
  status: string;
  runtime: string;
  phase: string;
  join_url: string;
  consent_mode: ConsentMode;
  consent_state: ConsentState;
  started_at: string | null;
  ended_at: string | null;
  exit_code: number | null;
  error: string | null;
  stop_reason: string | null;
  container: string | null;
}

// ---------------------------------------------------------------------------
// Playground: a whole meeting from the browser, no microphone or phone needed
// ---------------------------------------------------------------------------

export interface ScenarioTurn {
  speaker: string;
  text: string;
  /** What a correct delegate does at this line; shown beside it. */
  expect: string;
}

export interface Scenario {
  key: string;
  title: string;
  summary: string;
  participants: string[];
  turns: ScenarioTurn[];
}

export type PlaygroundEventKind =
  | "system"
  | "utterance"
  | "delegate"
  | "silent"
  | "approval_requested"
  | "approval_resolved";

export interface PlaygroundEvent {
  seq: number;
  kind: PlaygroundEventKind;
  at: string;
  speaker?: string;
  text?: string;
  reason?: string;
  state?: DecisionState;
  turn_type?: string | null;
  latency_ms?: number;
  followup?: boolean;
  request_id?: string;
  topic?: string | null;
  outcome?: string;
}

export interface PlaygroundChoices {
  approve: string;
  reject: string;
  soften: string;
}

export interface PlaygroundApproval {
  request_id: string;
  topic: string | null;
  tier: PolicyTier | null;
  speaker: string | null;
  heard: string;
  proposed: string;
  reason: string;
  pending: boolean;
  outcome: string | null;
  created_at: string;
  choices: PlaygroundChoices | null;
}

export interface PlaygroundSession {
  meeting_id: string;
  principal: string;
  participants: string[];
  started_at: string;
  concluded: boolean;
  /** Which model family is driving the delegate, e.g. "gemini (fast: ..., smart: ...)". */
  model: string;
  events: PlaygroundEvent[];
  approvals: PlaygroundApproval[];
  commitments: Commitment[];
  outbox: string[];
  report: PostMeetingReport | null;
}

export interface PlaygroundTurnResult {
  state: DecisionState;
  response: string | null;
  decision: PolicyDecision;
  turn_type: string | null;
  guard_verdict: string | null;
  commitment_ids: string[];
  /** Filled in client-side from the turn response, for the inspector. */
  latency_ms?: number;
}

export interface PlaygroundTurnOut {
  result: PlaygroundTurnResult;
  latency_ms: number;
  session: PlaygroundSession;
}

export interface SampleDataResult {
  loaded: string[];
  skipped: string[];
  dossier: string;
  index_size: number;
}
