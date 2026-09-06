import type {
  ExportFormat,
  LiveKitToken,
  Segment,
  Session,
  SessionRuntime,
  TranslationSegment,
} from "@/types/session";
import type { HostActionConfirmation, HostActionDescriptor, HostActionPreview, HostActionRequest, HostActionResult } from "@/types/host-actions";
import type {
  CaptionInputType,
  CaptionRun,
  ManagedRoom,
} from "@/types/room";
import type {
  PackageSummary,
  PackageValidation,
  TranscriptPackage,
} from "@/types/packages";
import type {
  RevisionExportFormat,
  RevisionSummary,
  TranscriptRevision,
  TranscriptRevisionContent,
} from "@/types/revisions";
import type {
  ArtifactKind,
  ArtifactContent,
  ArtifactExportFormat,
  DerivedArtifact,
  ProcessingJob,
} from "@/types/artifacts";
import type {
  AssistantApprovalRequest,
  AssistantCancelRequest,
  AssistantEventsPage,
  AssistantExecutionDetail,
  AssistantInputRequest,
  AssistantOperationResponse,
  AssistantSessionState,
  AssistantTurnAccepted,
  AssistantTurnCreate,
  JsonValue,
  MeetingMark,
  MeetingMarkCreate,
  MeetingMarkPatch,
  MeetingStateResponse,
} from "@/types/assistant";
import type {
  BuiltinPluginSummary,
  InstalledPlugin,
  HostHistorySource,
  HostHistoryPage,
  MediaSessionBridge,
  PluginCommandInput,
  PluginDocumentDetail,
  PluginDocumentExportFormat,
  PluginDocumentSummary,
  PluginGrantInput,
  PluginFrameworkHealth,
  PluginInspection,
  PluginViewEnvelope,
} from "@/types/plugins";

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ??
  "http://localhost:8000";

export class APIError extends Error {
  constructor(message: string, readonly status: number) { super(message); this.name = "APIError"; }
}

async function request<T>(path: string, init: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...init.headers,
    },
  });

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as { detail?: JsonValue };
      if (typeof body.detail === "string") {
        detail = body.detail;
      } else if (
        body.detail !== null &&
        typeof body.detail === "object" &&
        !Array.isArray(body.detail)
      ) {
        const message = body.detail.message;
        const code = body.detail.code;
        detail =
          typeof message === "string"
            ? message
            : typeof code === "string"
              ? code
              : detail;
      }
    } catch {
      // Preserve the HTTP status when the response is not JSON.
    }
    throw new APIError(detail, response.status);
  }

  if (response.status === 204) {
    return undefined as T;
  }
  try {
    return (await response.json()) as T;
  } catch {
    throw new APIError("服务响应无法解析，请重试原请求", 502);
  }
}

function pluginAdminHeaders(token: string): HeadersInit {
  return { "X-Plugin-Admin-Token": token };
}

const hostHeaders = (nonce?: string) => ({ "X-Assistant-UI": "1", ...(nonce ? { "X-Assistant-UI-Nonce": nonce } : {}) });
export function createAssistantUIContext(): Promise<{ ui_nonce: string }> {
  return request("/api/assistant-actions/ui-context", { method: "POST", headers: hostHeaders(), body: "{}" });
}
export function prepareHostAction(mediaId: string, input: HostActionRequest, nonce: string, history = false): Promise<HostActionPreview> {
  return request(`/api/media-sessions/${encodeURIComponent(mediaId)}/${history ? "assistant-history/prepare-cancel" : "assistant-actions/prepare"}`, {
    method: "POST", headers: hostHeaders(nonce), body: JSON.stringify(input),
  });
}
export function confirmHostAction(mediaId: string, input: HostActionConfirmation, nonce: string): Promise<HostActionResult> {
  return request(`/api/media-sessions/${encodeURIComponent(mediaId)}/assistant-actions/confirm`, {
    method: "POST", headers: hostHeaders(nonce), body: JSON.stringify(input),
  });
}
export function getHostActionDescriptors(mediaId: string): Promise<HostActionDescriptor[]> {
  return request(`/api/media-sessions/${encodeURIComponent(mediaId)}/assistant-actions`, { method: "GET" });
}
export function getAssistantHistorySources(sessionId: string): Promise<HostHistorySource[]> {
  return request(`/api/sessions/${encodeURIComponent(sessionId)}/assistant-history-sources`, { method: "GET" });
}
export function getHostHistory(source: HostHistorySource, offset = 0, after = 0, executionId = ""): Promise<HostHistoryPage> {
  const query = new URLSearchParams({ offset: String(offset), after: String(after), plugin_id: source.plugin_id });
  if (executionId) query.set("execution_id", executionId);
  return request(`/api/media-sessions/${encodeURIComponent(source.media_session_id)}/assistant-history?${query}`, { method: "GET" });
}

export async function inspectPluginPackage(
  file: File,
  adminToken: string,
): Promise<PluginInspection> {
  const response = await fetch(`${API_BASE_URL}/api/plugins/packages:inspect`, {
    method: "POST",
    headers: {
      "Content-Type": "application/vnd.matinier.plugin+zip",
      ...pluginAdminHeaders(adminToken),
    },
    body: file,
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // Preserve the HTTP status when the response is not JSON.
    }
    throw new Error(detail);
  }
  return (await response.json()) as PluginInspection;
}

export function confirmPluginInstall(
  inspection: PluginInspection,
  acceptedPermissions: string[],
  trustPublisher: boolean,
  adminToken: string,
): Promise<InstalledPlugin> {
  return request<InstalledPlugin>("/api/plugins/installations", {
    method: "POST",
    headers: pluginAdminHeaders(adminToken),
    body: JSON.stringify({
      ticket_id: inspection.ticket_id,
      accepted_permissions: acceptedPermissions,
      trust_publisher: trustPublisher,
      approved_publisher_fingerprint: trustPublisher
        ? inspection.publisher_fingerprint
        : null,
    }),
  });
}

export function listPlugins(): Promise<InstalledPlugin[]> {
  return request<InstalledPlugin[]>("/api/plugins", { method: "GET" });
}

export function listBuiltinPlugins(): Promise<BuiltinPluginSummary[]> {
  return request<BuiltinPluginSummary[]>("/api/plugins/builtins", {
    method: "GET",
  });
}

export async function getPluginFrameworkHealth(): Promise<PluginFrameworkHealth> {
  const response = await fetch(`${API_BASE_URL}/health/ready`, {
    method: "GET",
    headers: { "Content-Type": "application/json" },
  });
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new Error("插件框架状态不可用");
  }
  if (typeof body !== "object" || body === null || !("plugins" in body)) {
    throw new Error("插件框架状态不可用");
  }
  const plugins = (body as { plugins?: unknown }).plugins;
  if (
    typeof plugins !== "object" ||
    plugins === null ||
    typeof (plugins as { framework_enabled?: unknown }).framework_enabled !== "boolean"
  ) {
    throw new Error("插件框架状态不可用");
  }
  return plugins as PluginFrameworkHealth;
}

export function inspectBuiltinPlugin(
  pluginId: string,
  adminToken: string,
): Promise<PluginInspection> {
  return request<PluginInspection>(
    `/api/plugins/builtins/${encodeURIComponent(pluginId)}/packages:inspect`,
    { method: "POST", headers: pluginAdminHeaders(adminToken) },
  );
}

export function enablePlugin(
  pluginId: string,
  adminToken: string,
): Promise<InstalledPlugin> {
  return request<InstalledPlugin>(
    `/api/plugins/${encodeURIComponent(pluginId)}/enable`,
    { method: "POST", headers: pluginAdminHeaders(adminToken) },
  );
}

export function disablePlugin(
  pluginId: string,
  adminToken: string,
): Promise<InstalledPlugin> {
  return request<InstalledPlugin>(
    `/api/plugins/${encodeURIComponent(pluginId)}/disable`,
    { method: "POST", headers: pluginAdminHeaders(adminToken) },
  );
}

export function uninstallPlugin(
  pluginId: string,
  adminToken: string,
): Promise<void> {
  return request<void>(`/api/plugins/${encodeURIComponent(pluginId)}`, {
    method: "DELETE",
    headers: pluginAdminHeaders(adminToken),
  });
}

export function createPluginGrant(
  pluginId: string,
  input: PluginGrantInput,
  adminToken: string,
): Promise<{ grant_id: string; status: string }> {
  return request(`/api/plugins/${encodeURIComponent(pluginId)}/grants`, {
    method: "POST",
    headers: pluginAdminHeaders(adminToken),
    body: JSON.stringify(input),
  });
}

export function resolveMediaSession(
  legacySessionId: string,
): Promise<MediaSessionBridge> {
  return request(
    `/api/sessions/${encodeURIComponent(legacySessionId)}/media-session`,
    { method: "GET" },
  );
}

export function listPluginViews(
  mediaSessionId: string,
): Promise<PluginViewEnvelope[]> {
  return request(
    `/api/media-sessions/${encodeURIComponent(mediaSessionId)}/plugin-views`,
    { method: "GET" },
  );
}

export function executePluginCommand(
  mediaSessionId: string,
  input: PluginCommandInput,
): Promise<{ accepted: boolean; command_id: string }> {
  return request(
    `/api/media-sessions/${encodeURIComponent(mediaSessionId)}/plugin-commands`,
    { method: "POST", body: JSON.stringify(input) },
  );
}

export function listPluginDocuments(
  mediaSessionId: string,
): Promise<PluginDocumentSummary[]> {
  return request(
    `/api/media-sessions/${encodeURIComponent(mediaSessionId)}/plugin-documents`,
    { method: "GET" },
  );
}

export function getPluginDocument(
  documentId: string,
): Promise<PluginDocumentDetail> {
  return request(
    `/api/plugin-documents/${encodeURIComponent(documentId)}`,
    { method: "GET" },
  );
}

export function getPluginDocumentExportUrl(
  documentId: string,
  format: PluginDocumentExportFormat,
): string {
  if (documentId.length === 0) {
    throw new Error("Plugin document ID is required");
  }
  if (format !== "markdown" && format !== "json") {
    throw new Error("Unsupported plugin document export format");
  }
  return `${API_BASE_URL}/api/plugin-documents/${encodeURIComponent(documentId)}/export?format=${format}`;
}

export function createSession(input: {
  source_type: "empty" | "file";
  source_name: string;
  language: string;
}): Promise<Session> {
  return request<Session>("/api/sessions", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function getSession(sessionId: string): Promise<Session> {
  return request<Session>(`/api/sessions/${encodeURIComponent(sessionId)}`, {
    method: "GET",
  });
}

export function getSessionRuntime(sessionId: string): Promise<SessionRuntime> {
  return request<SessionRuntime>(
    `/api/sessions/${encodeURIComponent(sessionId)}/runtime`,
    { method: "GET" },
  );
}

export function listSessions(): Promise<Session[]> {
  return request<Session[]>("/api/sessions", { method: "GET" });
}

export function getSegments(sessionId: string): Promise<Segment[]> {
  return request<Segment[]>(
    `/api/sessions/${encodeURIComponent(sessionId)}/segments`,
    { method: "GET" },
  );
}

export function getTranslations(
  sessionId: string,
): Promise<TranslationSegment[]> {
  return request<TranslationSegment[]>(
    `/api/sessions/${encodeURIComponent(sessionId)}/translations`,
    { method: "GET" },
  );
}

export function createLiveKitToken(input: {
  session_id: string;
  participant_identity?: string;
}): Promise<LiveKitToken> {
  return request<LiveKitToken>("/api/livekit/token", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function getSessionExportUrl(
  sessionId: string,
  format: ExportFormat,
): string {
  const encodedSessionId = encodeURIComponent(sessionId);
  return `${API_BASE_URL}/api/sessions/${encodedSessionId}/export?format=${format}`;
}

export function buildSessionPackage(
  sessionId: string,
): Promise<TranscriptPackage> {
  return request<TranscriptPackage>(
    `/api/sessions/${encodeURIComponent(sessionId)}/packages`,
    { method: "POST" },
  );
}

export function listSessionPackages(
  sessionId: string,
): Promise<PackageSummary[]> {
  return request<PackageSummary[]>(
    `/api/sessions/${encodeURIComponent(sessionId)}/packages`,
    { method: "GET" },
  );
}

export function getPackage(packageId: string): Promise<TranscriptPackage> {
  return request<TranscriptPackage>(
    `/api/packages/${encodeURIComponent(packageId)}`,
    { method: "GET" },
  );
}

export function validatePackage(
  packageId: string,
): Promise<PackageValidation> {
  return request<PackageValidation>(
    `/api/packages/${encodeURIComponent(packageId)}/validate`,
    { method: "POST" },
  );
}

export function getPackageExportUrl(packageId: string): string {
  return `${API_BASE_URL}/api/packages/${encodeURIComponent(packageId)}/export`;
}

export function createPackageRevision(
  packageId: string,
  changeSummary?: string,
): Promise<TranscriptRevision> {
  return request<TranscriptRevision>(
    `/api/packages/${encodeURIComponent(packageId)}/revisions`,
    {
      method: "POST",
      body: JSON.stringify({ change_summary: changeSummary || null }),
    },
  );
}

export function listSessionRevisions(
  sessionId: string,
): Promise<RevisionSummary[]> {
  return request<RevisionSummary[]>(
    `/api/sessions/${encodeURIComponent(sessionId)}/revisions`,
    { method: "GET" },
  );
}

export function getRevision(revisionId: string): Promise<TranscriptRevision> {
  return request<TranscriptRevision>(
    `/api/revisions/${encodeURIComponent(revisionId)}`,
    { method: "GET" },
  );
}

export function createRevisionVersion(
  revisionId: string,
  content: TranscriptRevisionContent,
  changeSummary?: string,
): Promise<TranscriptRevision> {
  return request<TranscriptRevision>(
    `/api/revisions/${encodeURIComponent(revisionId)}/versions`,
    {
      method: "POST",
      body: JSON.stringify({
        content,
        change_summary: changeSummary || null,
      }),
    },
  );
}

export function approveRevision(
  revisionId: string,
): Promise<TranscriptRevision> {
  return request<TranscriptRevision>(
    `/api/revisions/${encodeURIComponent(revisionId)}/approve`,
    { method: "POST" },
  );
}

export function buildRevisionPackage(
  revisionId: string,
): Promise<TranscriptPackage> {
  return request<TranscriptPackage>(
    `/api/revisions/${encodeURIComponent(revisionId)}/packages`,
    { method: "POST" },
  );
}

export function getRevisionExportUrl(
  revisionId: string,
  format: RevisionExportFormat,
): string {
  return `${API_BASE_URL}/api/revisions/${encodeURIComponent(revisionId)}/export?format=${format}`;
}

export function createProcessingJob(
  packageId: string,
  artifactKind: ArtifactKind,
  options: Record<string, unknown> = {},
  targetArtifactId?: string,
): Promise<ProcessingJob> {
  return request<ProcessingJob>(
    `/api/packages/${encodeURIComponent(packageId)}/jobs`,
    {
      method: "POST",
      body: JSON.stringify({
        artifact_kind: artifactKind,
        options,
        target_artifact_id: targetArtifactId ?? null,
      }),
    },
  );
}

export function listPackageJobs(packageId: string): Promise<ProcessingJob[]> {
  return request<ProcessingJob[]>(
    `/api/packages/${encodeURIComponent(packageId)}/jobs`,
    { method: "GET" },
  );
}

export function getProcessingJob(jobId: string): Promise<ProcessingJob> {
  return request<ProcessingJob>(
    `/api/processing-jobs/${encodeURIComponent(jobId)}`,
    { method: "GET" },
  );
}

export function cancelProcessingJob(jobId: string): Promise<ProcessingJob> {
  return request<ProcessingJob>(
    `/api/processing-jobs/${encodeURIComponent(jobId)}/cancel`,
    { method: "POST" },
  );
}

export function listPackageArtifacts(
  packageId: string,
): Promise<DerivedArtifact[]> {
  return request<DerivedArtifact[]>(
    `/api/packages/${encodeURIComponent(packageId)}/artifacts`,
    { method: "GET" },
  );
}

export function getArtifact(artifactId: string): Promise<DerivedArtifact> {
  return request<DerivedArtifact>(
    `/api/artifacts/${encodeURIComponent(artifactId)}`,
    { method: "GET" },
  );
}

export function createArtifactVersion(
  artifactId: string,
  content: ArtifactContent,
): Promise<DerivedArtifact> {
  return request<DerivedArtifact>(
    `/api/artifacts/${encodeURIComponent(artifactId)}/versions`,
    {
      method: "POST",
      body: JSON.stringify({ content }),
    },
  );
}

export function approveArtifact(
  artifactId: string,
): Promise<DerivedArtifact> {
  return request<DerivedArtifact>(
    `/api/artifacts/${encodeURIComponent(artifactId)}/approve`,
    { method: "POST" },
  );
}

export function listArtifactVersions(
  artifactId: string,
): Promise<DerivedArtifact[]> {
  return request<DerivedArtifact[]>(
    `/api/artifacts/${encodeURIComponent(artifactId)}/history`,
    { method: "GET" },
  );
}

export function listApprovedArtifacts(
  packageId: string,
): Promise<DerivedArtifact[]> {
  return request<DerivedArtifact[]>(
    `/api/packages/${encodeURIComponent(packageId)}/approved-artifacts`,
    { method: "GET" },
  );
}

export function getArtifactExportUrl(
  artifactId: string,
  format: ArtifactExportFormat,
): string {
  return `${API_BASE_URL}/api/artifacts/${encodeURIComponent(artifactId)}/export?format=${format}`;
}

export function createRoom(displayName: string): Promise<ManagedRoom> {
  return request<ManagedRoom>("/api/rooms", {
    method: "POST",
    body: JSON.stringify({ display_name: displayName }),
  });
}

export function listRooms(): Promise<ManagedRoom[]> {
  return request<ManagedRoom[]>("/api/rooms", { method: "GET" });
}

export function updateRoom(
  roomId: string,
  displayName: string,
): Promise<ManagedRoom> {
  return request<ManagedRoom>(`/api/rooms/${encodeURIComponent(roomId)}`, {
    method: "PATCH",
    body: JSON.stringify({ display_name: displayName }),
  });
}

export function closeRoom(roomId: string): Promise<ManagedRoom> {
  return request<ManagedRoom>(
    `/api/rooms/${encodeURIComponent(roomId)}/close`,
    { method: "POST" },
  );
}

export function createRoomToken(
  roomId: string,
  participantIdentity?: string,
): Promise<LiveKitToken> {
  return request<LiveKitToken>(
    `/api/rooms/${encodeURIComponent(roomId)}/token`,
    {
      method: "POST",
      body: JSON.stringify({
        participant_identity: participantIdentity || undefined,
      }),
    },
  );
}

export function listCaptionRuns(roomId: string): Promise<CaptionRun[]> {
  return request<CaptionRun[]>(
    `/api/rooms/${encodeURIComponent(roomId)}/caption-runs`,
    { method: "GET" },
  );
}

export function createCaptionRun(
  roomId: string,
  input: {
    source_type: Exclude<CaptionInputType, "hls">;
    source_name: string;
    language: string;
    target_language: string | null;
  },
): Promise<CaptionRun> {
  return request<CaptionRun>(
    `/api/rooms/${encodeURIComponent(roomId)}/caption-runs`,
    {
      method: "POST",
      body: JSON.stringify(input),
    },
  );
}

export function startHLSInput(
  roomId: string,
  input: {
    url: string;
    language: string;
    target_language: string | null;
  },
): Promise<CaptionRun> {
  return request<CaptionRun>(
    `/api/rooms/${encodeURIComponent(roomId)}/hls-inputs`,
    {
      method: "POST",
      body: JSON.stringify(input),
    },
  );
}

export function stopHLSInput(
  roomId: string,
  sessionId: string,
): Promise<CaptionRun> {
  return request<CaptionRun>(
    `/api/rooms/${encodeURIComponent(roomId)}/hls-inputs/${encodeURIComponent(sessionId)}/stop`,
    { method: "POST" },
  );
}

export function cancelCaptionRun(
  roomId: string,
  sessionId: string,
): Promise<CaptionRun> {
  return request<CaptionRun>(
    `/api/rooms/${encodeURIComponent(roomId)}/caption-runs/${encodeURIComponent(sessionId)}/cancel`,
    { method: "POST" },
  );
}

export function removeRoomParticipant(
  roomId: string,
  participantIdentity: string,
): Promise<void> {
  return request<void>(
    `/api/rooms/${encodeURIComponent(roomId)}/participants/${encodeURIComponent(participantIdentity)}/remove`,
    { method: "POST" },
  );
}

export function createAssistantTurn(
  sessionId: string,
  input: AssistantTurnCreate,
): Promise<AssistantTurnAccepted> {
  return request<AssistantTurnAccepted>(
    `/api/sessions/${encodeURIComponent(sessionId)}/assistant/turns`,
    {
      method: "POST",
      body: JSON.stringify(input),
    },
  );
}

export function getAssistantState(
  sessionId: string,
): Promise<AssistantSessionState> {
  return request<AssistantSessionState>(
    `/api/sessions/${encodeURIComponent(sessionId)}/assistant/state`,
    { method: "GET" },
  );
}

export function listAssistantEvents(
  sessionId: string,
  after: number,
  limit = 100,
): Promise<AssistantEventsPage> {
  const parameters = new URLSearchParams({
    after: String(after),
    limit: String(Math.min(100, Math.max(1, limit))),
  });
  return request<AssistantEventsPage>(
    `/api/sessions/${encodeURIComponent(sessionId)}/assistant/events?${parameters}`,
    { method: "GET" },
  );
}

export function getAssistantExecution(
  executionId: string,
): Promise<AssistantExecutionDetail> {
  return request<AssistantExecutionDetail>(
    `/api/assistant/executions/${encodeURIComponent(executionId)}`,
    { method: "GET" },
  );
}

export function submitAssistantInput(
  executionId: string,
  input: AssistantInputRequest,
): Promise<AssistantOperationResponse> {
  return request<AssistantOperationResponse>(
    `/api/assistant/executions/${encodeURIComponent(executionId)}/input`,
    {
      method: "POST",
      body: JSON.stringify(input),
    },
  );
}

export function cancelAssistantExecution(
  executionId: string,
  input: AssistantCancelRequest,
): Promise<AssistantOperationResponse> {
  return request<AssistantOperationResponse>(
    `/api/assistant/executions/${encodeURIComponent(executionId)}/cancel`,
    {
      method: "POST",
      body: JSON.stringify(input),
    },
  );
}

export function approveAssistantAction(
  executionId: string,
  approvalId: string,
  input: AssistantApprovalRequest,
): Promise<AssistantOperationResponse> {
  return request<AssistantOperationResponse>(
    `/api/assistant/executions/${encodeURIComponent(executionId)}/approvals/${encodeURIComponent(approvalId)}/approve`,
    {
      method: "POST",
      body: JSON.stringify(input),
    },
  );
}

export function rejectAssistantAction(
  executionId: string,
  approvalId: string,
  input: AssistantApprovalRequest,
): Promise<AssistantOperationResponse> {
  return request<AssistantOperationResponse>(
    `/api/assistant/executions/${encodeURIComponent(executionId)}/approvals/${encodeURIComponent(approvalId)}/reject`,
    {
      method: "POST",
      body: JSON.stringify(input),
    },
  );
}

export function getMeetingState(
  sessionId: string,
): Promise<MeetingStateResponse> {
  return request<MeetingStateResponse>(
    `/api/sessions/${encodeURIComponent(sessionId)}/meeting-state`,
    { method: "GET" },
  );
}

export function listMeetingMarks(sessionId: string): Promise<MeetingMark[]> {
  return request<MeetingMark[]>(
    `/api/sessions/${encodeURIComponent(sessionId)}/marks`,
    { method: "GET" },
  );
}

export function createMeetingMark(
  sessionId: string,
  input: MeetingMarkCreate,
): Promise<MeetingMark> {
  return request<MeetingMark>(
    `/api/sessions/${encodeURIComponent(sessionId)}/marks`,
    {
      method: "POST",
      body: JSON.stringify(input),
    },
  );
}

export function updateMeetingMark(
  sessionId: string,
  markId: string,
  input: MeetingMarkPatch,
): Promise<MeetingMark> {
  return request<MeetingMark>(
    `/api/sessions/${encodeURIComponent(sessionId)}/marks/${encodeURIComponent(markId)}`,
    {
      method: "PATCH",
      body: JSON.stringify(input),
    },
  );
}
