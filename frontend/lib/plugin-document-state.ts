import type {
  PluginDocumentCompleteness,
  PluginDocumentSummary,
  PluginDocumentTrigger,
} from "@/types/plugins";

export interface PluginDocumentGroup {
  key: string;
  pluginId: string;
  identityKey: string;
  language: string;
  newest: PluginDocumentSummary;
  latestInterim: PluginDocumentSummary | null;
  latestComplete: PluginDocumentSummary | null;
  versions: PluginDocumentSummary[];
}

export interface PluginDocumentState {
  status: "idle" | "loading" | "refreshing" | "ready" | "error";
  groups: PluginDocumentGroup[];
  error: string | null;
}

export type PluginDocumentAction =
  | { type: "loading" }
  | { type: "refreshing" }
  | { type: "loaded"; documents: PluginDocumentSummary[] }
  | { type: "failed"; message: string };

const COMPLETENESS = new Set<PluginDocumentCompleteness>([
  "interim",
  "complete",
  "partial_terminal",
]);
const TRIGGERS = new Set<PluginDocumentTrigger>([
  "manual",
  "session_completed",
  "session_failed",
  "session_cancelled",
]);

function normalizeDocument(
  value: PluginDocumentSummary,
): PluginDocumentSummary {
  return {
    document_id: String(value.document_id),
    plugin_id: String(value.plugin_id),
    plugin_version: String(value.plugin_version),
    media_session_id: String(value.media_session_id),
    source_package_id: String(value.source_package_id),
    source_package_version: Number(value.source_package_version),
    source_package_hash: String(value.source_package_hash),
    identity_key: String(value.identity_key),
    document_version: Number(value.document_version),
    schema_name: String(value.schema_name),
    schema_version: String(value.schema_version),
    language: String(value.language),
    trigger: TRIGGERS.has(value.trigger) ? value.trigger : "manual",
    completeness: COMPLETENESS.has(value.completeness)
      ? value.completeness
      : "interim",
    status: String(value.status),
    content_hash: String(value.content_hash),
    created_at: String(value.created_at),
  };
}

function compareVersions(
  left: PluginDocumentSummary,
  right: PluginDocumentSummary,
): number {
  return (
    right.document_version - left.document_version ||
    right.created_at.localeCompare(left.created_at) ||
    left.document_id.localeCompare(right.document_id)
  );
}

export function groupPluginDocuments(
  documents: PluginDocumentSummary[],
): PluginDocumentGroup[] {
  const grouped = new Map<string, PluginDocumentSummary[]>();
  for (const source of documents) {
    const document = normalizeDocument(source);
    const key = [
      document.plugin_id,
      document.identity_key,
      document.language,
    ].join("\u001f");
    const versions = grouped.get(key) ?? [];
    versions.push(document);
    grouped.set(key, versions);
  }

  return [...grouped.entries()]
    .map(([key, versions]) => {
      versions.sort(compareVersions);
      const newest = versions[0];
      if (newest === undefined) {
        throw new Error("Plugin document group cannot be empty");
      }
      return {
        key,
        pluginId: newest.plugin_id,
        identityKey: newest.identity_key,
        language: newest.language,
        newest,
        latestInterim:
          versions.find((document) => document.completeness === "interim") ??
          null,
        latestComplete:
          versions.find((document) => document.completeness === "complete") ??
          null,
        versions,
      };
    })
    .sort((left, right) => left.key.localeCompare(right.key));
}

export function createPluginDocumentState(): PluginDocumentState {
  return { status: "idle", groups: [], error: null };
}

export function reducePluginDocumentState(
  state: PluginDocumentState,
  action: PluginDocumentAction,
): PluginDocumentState {
  switch (action.type) {
    case "loading":
      return { ...state, status: "loading", error: null };
    case "refreshing":
      return { ...state, status: "refreshing", error: null };
    case "loaded":
      return {
        status: "ready",
        groups: groupPluginDocuments(action.documents),
        error: null,
      };
    case "failed":
      return { ...state, status: "error", error: action.message };
  }
}
