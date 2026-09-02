import type { PluginUIViewDocument, PluginUISurface } from "./plugin-ui";

export interface PluginInspection {
  ticket_id: string;
  plugin_id: string;
  version: string;
  name: string;
  publisher_name: string;
  publisher_fingerprint: string | null;
  publisher_trusted: boolean;
  signature_status: string;
  permissions: string[];
  content_digest: string;
  manifest_hash: string;
  expires_at: string;
}

export interface InstalledPlugin {
  plugin_id: string;
  name: string;
  status: string;
  preferred_version: string;
  versions: string[];
  permissions: string[];
  runtime_status: string;
  quarantine_reason: string | null;
}

export interface BuiltinPluginSummary {
  id: string;
  name: string;
  description: string;
  version: string;
  dynamic_build_available: boolean;
}

export interface PluginFrameworkHealth {
  framework_enabled: boolean;
  container_runtime_available: boolean;
  installed_count: number;
  enabled_count: number;
  ready_count: number;
  quarantined_count: number;
  media_projector_status: string;
  media_projector_lag: number;
  rpc_pending_count: number;
}

export type AssistantCatalogStatus =
  | "quarantined"
  | "crashed"
  | "degraded"
  | "disabled"
  | "waiting"
  | "ready"
  | "historical";

export interface AssistantCatalogEntry {
  pluginId: string;
  name: string;
  installed: InstalledPlugin | null;
  views: PluginViewEnvelope[];
  documents: PluginDocumentSummary[];
  historySources?: HostHistorySource[];
  status: AssistantCatalogStatus;
}

export interface HostHistorySource {
  source_kind: "host_history";
  plugin_id: string;
  name: string;
  media_session_id: string;
  legacy_session_id: string;
}
export interface HostHistoryPage {
  ui_view: unknown;
  view: { offset: number; next_offset: number; has_more: boolean; next_cursor: number; has_more_events: boolean };
  executions: { value: string; label: string }[];
}

export type AssistantViewInputState = Record<
  string,
  Record<string, unknown>
>;

export interface PluginGrantInput {
  media_session_id: string | null;
  capability: string;
  effect: "read" | "local_write" | "network" | "external_write";
  scope: Record<string, unknown>;
  ttl_seconds: number;
}

export interface MediaSessionBridge {
  media_session_id: string;
  legacy_session_id: string | null;
  mode: string;
  source_kind: string;
  status: string;
}

export interface PluginViewEnvelope {
  plugin_id: string;
  plugin_version: string;
  session_scope: string;
  surface: PluginUISurface;
  view_id: string;
  view_version: number;
  allowed_commands: string[];
  view: unknown;
}

export interface ParsedPluginViewEnvelope
  extends Omit<PluginViewEnvelope, "view"> {
  view: PluginUIViewDocument;
  safe: boolean;
}

export interface PluginCommandInput {
  plugin_id: string;
  plugin_version: string;
  session_scope: string;
  surface: PluginUISurface;
  view_id: string;
  expected_view_version: number;
  action_id: string;
  values: Record<string, unknown>;
}

export type PluginDocumentTrigger =
  | "manual"
  | "session_completed"
  | "session_failed"
  | "session_cancelled";

export type PluginDocumentCompleteness =
  | "interim"
  | "complete"
  | "partial_terminal";

export interface PluginDocumentEvidenceRef {
  item_id: string;
  source_segment_ids: string[];
  start_ms: number;
  end_ms: number;
}

export interface PluginDocumentSummary {
  document_id: string;
  plugin_id: string;
  plugin_version: string;
  media_session_id: string;
  source_package_id: string;
  source_package_version: number;
  source_package_hash: string;
  identity_key: string;
  document_version: number;
  schema_name: string;
  schema_version: string;
  language: string;
  trigger: PluginDocumentTrigger;
  completeness: PluginDocumentCompleteness;
  status: string;
  content_hash: string;
  created_at: string;
}

export interface PluginDocumentDetail extends PluginDocumentSummary {
  content: Record<string, unknown>;
  markdown: string;
  evidence_refs: PluginDocumentEvidenceRef[];
}

export type PluginDocumentExportFormat = "markdown" | "json";
