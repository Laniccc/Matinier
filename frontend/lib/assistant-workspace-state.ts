import type {
  AssistantCatalogEntry,
  AssistantCatalogStatus,
  AssistantViewInputState,
  InstalledPlugin,
  PluginDocumentSummary,
  ParsedPluginViewEnvelope,
  PluginViewEnvelope,
  HostHistorySource,
} from "@/types/plugins";
import type { PluginUISurface } from "@/types/plugin-ui";
import type { Session } from "@/types/session";


interface CatalogSources {
  installedPlugins: InstalledPlugin[];
  views: PluginViewEnvelope[];
  documents: PluginDocumentSummary[];
  historySources?: HostHistorySource[];
}

export interface RoomAssistantSummary {
  connectedCount: number;
  errorCount: number;
  latestUpdateAt: string | null;
  href: string;
}

const FAILURE_STATUSES = new Set<AssistantCatalogStatus>([
  "quarantined",
  "crashed",
  "degraded",
]);

const SELECTION_PRIORITY: Record<AssistantCatalogStatus, number> = {
  ready: 0,
  degraded: 1,
  historical: 2,
  waiting: 3,
  disabled: 4,
  crashed: 5,
  quarantined: 6,
};

const QUERY_SELECTION = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$/;
const ACTIVE_SESSION_PRIORITY: Partial<Record<Session["status"], number>> = {
  running: 0,
  starting: 1,
  finalizing: 2,
  created: 3,
};


export function buildAssistantCatalog({
  installedPlugins,
  views,
  documents,
  historySources = [],
}: CatalogSources): AssistantCatalogEntry[] {
  const pluginIds = new Set<string>();
  const installedById = new Map<string, InstalledPlugin>();
  const viewsById = new Map<string, PluginViewEnvelope[]>();
  const documentsById = new Map<string, PluginDocumentSummary[]>();
  const historyById = new Map<string, HostHistorySource[]>();
  for (const source of historySources) {
    pluginIds.add(source.plugin_id);
    const group = historyById.get(source.plugin_id) ?? [];
    if (!group.some(item => item.media_session_id === source.media_session_id)) group.push(source);
    historyById.set(source.plugin_id, group);
  }

  for (const plugin of installedPlugins) {
    pluginIds.add(plugin.plugin_id);
    installedById.set(plugin.plugin_id, plugin);
  }
  for (const pluginView of views) {
    pluginIds.add(pluginView.plugin_id);
    const group = viewsById.get(pluginView.plugin_id) ?? [];
    group.push(pluginView);
    viewsById.set(pluginView.plugin_id, group);
  }
  for (const document of documents) {
    pluginIds.add(document.plugin_id);
    const group = documentsById.get(document.plugin_id) ?? [];
    group.push(document);
    documentsById.set(document.plugin_id, group);
  }

  return [...pluginIds]
    .map((pluginId): AssistantCatalogEntry => {
      const installed = installedById.get(pluginId) ?? null;
      const pluginViews = [...(viewsById.get(pluginId) ?? [])].sort(compareViews);
      const pluginDocuments = filterAssistantDocuments(
        documentsById.get(pluginId) ?? [],
        pluginId,
      );
      return {
        pluginId,
        name: installed?.name || historyById.get(pluginId)?.[0]?.name || pluginId,
        installed,
        views: pluginViews,
        documents: pluginDocuments,
        historySources: historyById.get(pluginId) ?? [],
        status: !installed && historyById.has(pluginId) ? "historical" : deriveAssistantStatus(installed, pluginViews, pluginDocuments),
      };
    })
    .sort(
      (left, right) =>
        left.name.localeCompare(right.name) ||
        left.pluginId.localeCompare(right.pluginId),
    );
}

export function selectAssistantPluginId(
  entries: AssistantCatalogEntry[],
  requestedPluginId: string | null | undefined,
): string | null {
  if (
    requestedPluginId &&
    entries.some((entry) => entry.pluginId === requestedPluginId)
  ) {
    return requestedPluginId;
  }
  const [fallback] = [...entries].sort(
    (left, right) =>
      SELECTION_PRIORITY[left.status] - SELECTION_PRIORITY[right.status] ||
      left.name.localeCompare(right.name) ||
      left.pluginId.localeCompare(right.pluginId),
  );
  return fallback?.pluginId ?? null;
}

export function selectAssistantPagePluginId(
  entries: AssistantCatalogEntry[],
  currentPluginId: string | null,
  requestedPluginId: string | null,
): string | null {
  if (entries.length === 0) return currentPluginId;
  if (
    requestedPluginId !== null &&
    !entries.some((entry) => entry.pluginId === requestedPluginId)
  ) {
    return requestedPluginId;
  }
  return selectAssistantPluginId(entries, currentPluginId);
}

export function normalizeAssistantQuerySelection(
  value: string | null | undefined,
): string | null {
  const normalized = value?.trim() ?? "";
  return QUERY_SELECTION.test(normalized) ? normalized : null;
}

export function selectAssistantSessionId(
  sessions: Session[],
  requestedSessionId: string | null | undefined,
): string | null {
  const requested = normalizeAssistantQuerySelection(requestedSessionId);
  if (requested && sessions.some((session) => session.id === requested)) {
    return requested;
  }
  const [selected] = [...sessions].sort((left, right) => {
    const leftPriority = ACTIVE_SESSION_PRIORITY[left.status] ?? 10;
    const rightPriority = ACTIVE_SESSION_PRIORITY[right.status] ?? 10;
    return (
      leftPriority - rightPriority ||
      right.created_at.localeCompare(left.created_at) ||
      left.id.localeCompare(right.id)
    );
  });
  return selected?.id ?? null;
}

export function isAssistantGenerationActive(
  expectedGeneration: number,
  activeGeneration: number,
  disposed: boolean,
): boolean {
  return !disposed && expectedGeneration === activeGeneration;
}

export function isAssistantRequestCurrent(
  request: number,
  latestRequest: number,
  expectedGeneration: number,
  activeGeneration: number,
  disposed: boolean,
): boolean {
  return (
    request === latestRequest &&
    isAssistantGenerationActive(expectedGeneration, activeGeneration, disposed)
  );
}

export function canExecuteAssistantView(
  view: ParsedPluginViewEnvelope,
): boolean {
  return view.safe;
}

export function summarizeRoomAssistants(
  entries: AssistantCatalogEntry[],
  legacySessionId: string | null,
  latestViewUpdateAt: string | null,
): RoomAssistantSummary {
  const documentUpdates = entries.flatMap((entry) =>
    entry.documents.map((document) => document.created_at),
  );
  const latestUpdateAt = [latestViewUpdateAt, ...documentUpdates]
    .filter((value): value is string => Boolean(value))
    .sort((left, right) => right.localeCompare(left))[0] ?? null;
  return {
    connectedCount: entries.filter(
      (entry) =>
        entry.views.length > 0 &&
        (entry.status === "ready" || entry.status === "degraded"),
    ).length,
    errorCount: entries.filter((entry) =>
      ["degraded", "crashed", "quarantined"].includes(entry.status),
    ).length,
    latestUpdateAt,
    href:
      legacySessionId === null
        ? "/assistants"
        : `/assistants?session=${encodeURIComponent(legacySessionId)}`,
  };
}

export function makeAssistantViewKey(
  mediaSessionId: string,
  pluginId: string,
  surface: PluginUISurface,
  viewId: string,
): string {
  return [mediaSessionId, pluginId, surface, viewId].join(":");
}

export function setAssistantViewInput(
  state: AssistantViewInputState,
  viewKey: string,
  name: string,
  value: unknown,
): AssistantViewInputState {
  return {
    ...state,
    [viewKey]: {
      ...(state[viewKey] ?? {}),
      [name]: value,
    },
  };
}

export function clearAssistantSessionInputs(
  state: AssistantViewInputState,
  mediaSessionId: string,
): AssistantViewInputState {
  const prefix = `${mediaSessionId}:`;
  return Object.fromEntries(
    Object.entries(state).filter(([key]) => !key.startsWith(prefix)),
  );
}

export function filterAssistantDocuments(
  documents: PluginDocumentSummary[],
  pluginId: string,
): PluginDocumentSummary[] {
  return documents
    .filter((document) => document.plugin_id === pluginId)
    .map((document) => ({
      document_id: String(document.document_id),
      plugin_id: String(document.plugin_id),
      plugin_version: String(document.plugin_version),
      media_session_id: String(document.media_session_id),
      source_package_id: String(document.source_package_id),
      source_package_version: Number(document.source_package_version),
      source_package_hash: String(document.source_package_hash),
      identity_key: String(document.identity_key),
      document_version: Number(document.document_version),
      schema_name: String(document.schema_name),
      schema_version: String(document.schema_version),
      language: String(document.language),
      trigger: document.trigger,
      completeness: document.completeness,
      status: String(document.status),
      content_hash: String(document.content_hash),
      created_at: String(document.created_at),
    }))
    .sort(
      (left, right) =>
        right.created_at.localeCompare(left.created_at) ||
        right.document_version - left.document_version ||
        left.document_id.localeCompare(right.document_id),
    );
}

function deriveAssistantStatus(
  installed: InstalledPlugin | null,
  views: PluginViewEnvelope[],
  documents: PluginDocumentSummary[],
): AssistantCatalogStatus {
  const runtimeStatus = installed?.runtime_status.toLowerCase();
  if (
    runtimeStatus &&
    FAILURE_STATUSES.has(runtimeStatus as AssistantCatalogStatus)
  ) {
    return runtimeStatus as AssistantCatalogStatus;
  }
  if (installed !== null && installed.status !== "enabled") return "disabled";
  if (views.length > 0) return "ready";
  if (installed !== null) return "waiting";
  if (documents.length > 0) return "historical";
  return "waiting";
}

function compareViews(left: PluginViewEnvelope, right: PluginViewEnvelope): number {
  return (
    left.surface.localeCompare(right.surface) ||
    left.view_id.localeCompare(right.view_id) ||
    right.view_version - left.view_version
  );
}
