export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonObject | JsonValue[];
export type JsonObject = { [key: string]: JsonValue };

export type AssistantProfile = "fast_turn" | "action_run";
export type FastTurnStatus =
  | "received"
  | "planning"
  | "tool_wait"
  | "responding"
  | "handed_off"
  | "completed"
  | "failed"
  | "cancelled";
export type ActionRunStatus =
  | "queued"
  | "planning"
  | "executing"
  | "observing"
  | "waiting_external"
  | "reconciling"
  | "needs_input"
  | "completed"
  | "partial"
  | "failed"
  | "cancelled";
export type AssistantExecutionStatus = FastTurnStatus | ActionRunStatus;

export type NeedsInputSummary = {
  question: string;
  choices: string[];
  evidence_refs: string[];
  error_code: string | null;
};

export type ExternalEffectsSummary = {
  confirmed: number;
  unknown: number;
  existing_actions_remain: boolean;
};

export type AssistantExecutionSummary = {
  execution_id: string;
  session_id: string;
  profile: AssistantProfile;
  root_execution_id: string;
  parent_execution_id: string | null;
  snapshot_id: string | null;
  grant_id: string | null;
  client_request_id: string | null;
  goal: string;
  status: AssistantExecutionStatus;
  state_version: number;
  step_count: number;
  result: JsonObject | null;
  needs_input: NeedsInputSummary | null;
  external_effects: ExternalEffectsSummary;
  error_code: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
};

export type AssistantTurnGrant = {
  actor_id: string;
  capabilities: ["task.create"];
  candidate_ids: string[];
  max_side_effects: number;
  expires_at: string;
  unresolved_identity_policy: "placeholder";
};

export type AssistantTurnCreate = {
  intent_mode: "ask" | "execute";
  message: string;
  client_request_id: string;
  actor_id: string;
  mark_ids: string[];
  allow_handoff: boolean;
  grant: AssistantTurnGrant | null;
};

export type AssistantTurnAccepted = {
  execution: AssistantExecutionSummary;
  event_cursor: number;
};

export type AssistantSessionState = {
  active_executions: AssistantExecutionSummary[];
  recent_terminal_executions: AssistantExecutionSummary[];
  snapshot_cursor: number;
};

export type AssistantEvent = {
  event_id: number;
  session_id: string;
  execution_id: string;
  root_execution_id: string;
  state_version: number;
  schema_version: number;
  event_type: string;
  phase: string | null;
  status: AssistantExecutionStatus;
  summary: string;
  payload: JsonObject;
  created_at: string;
};

export type AssistantEventsPage = {
  events: AssistantEvent[];
  next_cursor: number;
  has_more: boolean;
};

export type AssistantStepSummary = {
  sequence: number;
  kind: string;
  decision_summary: string | null;
  created_at: string;
};

export type AssistantToolCallSummary = {
  tool_call_id: string;
  tool_name: string;
  capability: string;
  effect: "read" | "local_write" | "external_write";
  status: string;
  external_reference: JsonObject | null;
  error_code: string | null;
  created_at: string;
  updated_at: string;
};

export type AssistantExecutionDetail = {
  execution: AssistantExecutionSummary;
  steps: AssistantStepSummary[];
  tool_calls: AssistantToolCallSummary[];
};

export type AssistantInputRequest = {
  client_operation_id: string;
  expected_state_version: number;
  input: string;
};

export type AssistantCancelRequest = {
  client_operation_id: string;
  expected_state_version: number;
};

export type AssistantOperationResponse = {
  execution: AssistantExecutionSummary;
};

export type MeetingStateStatus = "ready" | "lagging" | "stale" | "rebuilding";
export type MarkKind = "highlight" | "decision" | "action" | "conflict";
export type MarkOrigin = "manual" | "automatic";
export type MarkStatus = "candidate" | "accepted" | "dismissed";
export type ActionReadiness =
  | "detected"
  | "recordable"
  | "executable"
  | "fully_specified";
export type CandidateContentStatus =
  | "active"
  | "dismissed"
  | "superseded"
  | "cancelled";
export type CandidateExecutionStatus =
  | "not_requested"
  | "handed_off"
  | "executing"
  | "executed"
  | "failed";
export type GroundedValueOrigin =
  | "meeting_explicit"
  | "meeting_inferred"
  | "user_supplied"
  | "external_resolved"
  | "server_default";
export type GroundedValueResolution =
  | "known"
  | "missing"
  | "ambiguous"
  | "conflicting";
export type TaskPriority = "low" | "normal" | "high" | "urgent";

export type GroundedValue<T> = {
  value: T | null;
  origin: GroundedValueOrigin;
  resolution: GroundedValueResolution;
  evidence_message_ids: string[];
  confidence: number | null;
  explanation: string | null;
};

export type AssigneeValue = {
  spoken_text: string;
  linear_user_id: string | null;
  is_placeholder: boolean;
};

export type CaptionEvidenceMessage = {
  message_kind: "caption";
  message_id: string;
  session_id: string;
  actor_id: null;
  speaker_label: string | null;
  track_id: string;
  segment_id: string;
  segment_revision: number;
  language: string;
  raw_text: string;
  display_text: string;
  audio_start_ms: number | null;
  audio_end_ms: number | null;
  confidence: number | null;
  received_at_ms: number;
  finalized_at: string;
  content_hash: string;
};

export type UserInputEvidenceMessage = {
  message_kind: "user_input";
  message_id: string;
  session_id: string;
  actor_id: string;
  raw_text: string;
  display_text: string;
  created_at: string;
  content_hash: string;
};

export type EvidenceMessage = CaptionEvidenceMessage | UserInputEvidenceMessage;

export type ActionCandidate = {
  candidate_id: string;
  lineage_root_id: string;
  session_id: string;
  current_revision: number;
  readiness: ActionReadiness;
  content_status: CandidateContentStatus;
  execution_status: CandidateExecutionStatus;
  content: {
    title: GroundedValue<string>;
    deliverable: GroundedValue<string>;
    assignee: GroundedValue<AssigneeValue>;
    due_at: GroundedValue<string>;
    priority: GroundedValue<TaskPriority>;
    evidence_messages: EvidenceMessage[];
    blocking_conflict_message_ids: string[];
  };
  source_segment_ids: string[];
  evidence_revisions_current: boolean;
  derived_from_candidate_id: string | null;
  derived_from_revision: number | null;
  superseded_by_candidate_id: string | null;
};

export type MeetingStateItem = {
  item_id: string;
  text: string;
  source_segment_ids: string[];
  source_segment_revisions: Record<string, number>;
};

export type MeetingState = {
  session_id: string;
  version: number;
  topics: MeetingStateItem[];
  entities: Array<MeetingStateItem & { entity_type: string }>;
  decisions: MeetingStateItem[];
  action_candidates: ActionCandidate[];
  highlights: MeetingStateItem[];
  conflicts: MeetingStateItem[];
  user_concerns: MeetingStateItem[];
};

export type MeetingStateFreshness = {
  status: MeetingStateStatus;
  state_updated_at: string | null;
  latest_final_updated_at: string | null;
  projected_through: string | null;
  lag_ms: number;
  pending_segment_count: number;
  has_unprojected_tail: boolean;
  last_success_at: string | null;
  last_error_at: string | null;
  last_error_code: string | null;
};

export type MeetingStateResponse = {
  state: MeetingState;
  state_hash: string;
  source_frontier: JsonObject;
  freshness: MeetingStateFreshness;
};

export type MarkEvidenceRef = {
  segment_id: string;
  revision: number;
};

export type MeetingMark = {
  mark_id: string;
  session_id: string;
  origin: MarkOrigin;
  kind: MarkKind;
  status: MarkStatus;
  title: string;
  note: string | null;
  confidence: number | null;
  evidence: MarkEvidenceRef[];
  evidence_messages: EvidenceMessage[];
  audio_start_ms: number | null;
  audio_end_ms: number | null;
  source_state_version: number;
  created_at: string;
  updated_at: string;
};

export type MeetingMarkCreate = {
  kind: MarkKind;
  title: string;
  note: string | null;
  actor_id: string;
  evidence: MarkEvidenceRef[];
};

export type MeetingMarkPatch = {
  status: "accepted" | "dismissed";
  evidence: MarkEvidenceRef[] | null;
};

export type RootExecutionCard = {
  root_execution_id: string;
  execution_ids: string[];
};

export type PendingTurnRetry = {
  request: AssistantTurnCreate;
  reason: string;
};
