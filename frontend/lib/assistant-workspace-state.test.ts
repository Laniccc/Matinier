import { describe, expect, it } from "vitest";

import {
  buildAssistantCatalog,
  canExecuteAssistantView,
  clearAssistantSessionInputs,
  filterAssistantDocuments,
  isAssistantGenerationActive,
  isAssistantRequestCurrent,
  makeAssistantViewKey,
  normalizeAssistantQuerySelection,
  selectAssistantSessionId,
  selectAssistantPluginId,
  selectAssistantPagePluginId,
  setAssistantViewInput,
  summarizeRoomAssistants,
} from "./assistant-workspace-state";
import type {
  InstalledPlugin,
  ParsedPluginViewEnvelope,
  PluginDocumentSummary,
  PluginViewEnvelope,
} from "@/types/plugins";
import type { Session } from "@/types/session";


function installed(
  pluginId: string,
  overrides: Partial<InstalledPlugin> = {},
): InstalledPlugin {
  return {
    plugin_id: pluginId,
    name: pluginId,
    status: "enabled",
    preferred_version: "1.0.0",
    versions: ["1.0.0"],
    permissions: [],
    runtime_status: "ready",
    quarantine_reason: null,
    ...overrides,
  };
}

function view(pluginId: string, viewId = "main"): PluginViewEnvelope {
  return {
    plugin_id: pluginId,
    plugin_version: "1.0.0",
    session_scope: "session",
    surface: "panel",
    view_id: viewId,
    view_version: 1,
    allowed_commands: [],
    view: {},
  };
}

function document(
  pluginId: string,
  documentId: string,
): PluginDocumentSummary {
  return {
    document_id: documentId,
    plugin_id: pluginId,
    plugin_version: "1.0.0",
    media_session_id: "media-1",
    source_package_id: "package-1",
    source_package_version: 1,
    source_package_hash: "a".repeat(64),
    identity_key: "notes:zh-CN",
    document_version: 1,
    schema_name: "notes",
    schema_version: "1.0",
    language: "zh-CN",
    trigger: "manual",
    completeness: "interim",
    status: "published",
    content_hash: "b".repeat(64),
    created_at: "2026-08-29T01:00:00Z",
  };
}

function session(
  id: string,
  status: Session["status"],
  createdAt: string,
): Session {
  return {
    id,
    room_id: null,
    room_name: id,
    status,
    source_type: "screen",
    source_name: id,
    language: "en-US",
    target_language: "zh-CN",
    asr_provider: null,
    asr_model: null,
    translation_status: "running",
    translation_provider: null,
    translation_model: null,
    translation_error_code: null,
    translation_error_message: null,
    final_result_count: null,
    first_partial_latency_ms: null,
    average_final_latency_ms: null,
    provider_error_count: null,
    sent_audio_chunk_count: null,
    sent_audio_bytes: null,
    error_code: null,
    error_message: null,
    stop_reason: null,
    failure_code: null,
    failure_detail: null,
    started_at: null,
    ended_at: null,
    source_ended_at: null,
    translation_ended_at: null,
    created_at: createdAt,
  };
}


describe("assistant workspace state", () => {
  it("merges installed, view-only, and document-only plugins without dropping empty installations", () => {
    const entries = buildAssistantCatalog({
      installedPlugins: [
        installed("plugin.waiting", { name: "Waiting" }),
        installed("plugin.course", { name: "Course" }),
      ],
      views: [view("plugin.course"), view("plugin.view-only")],
      documents: [document("plugin.history", "doc-history")],
    });

    expect(entries.map((entry) => entry.pluginId).sort()).toEqual([
      "plugin.course",
      "plugin.history",
      "plugin.view-only",
      "plugin.waiting",
    ]);
    expect(entries.find((entry) => entry.pluginId === "plugin.waiting")?.views).toEqual([]);
    expect(entries.find((entry) => entry.pluginId === "plugin.history")?.installed).toBeNull();
    expect(entries.find((entry) => entry.pluginId === "plugin.history")?.status).toBe("historical");
  });

  it("keeps runtime failures ahead of waiting and ready inference", () => {
    const entries = buildAssistantCatalog({
      installedPlugins: [
        installed("plugin.quarantined", { runtime_status: "quarantined" }),
        installed("plugin.crashed", { runtime_status: "crashed" }),
        installed("plugin.degraded", { runtime_status: "degraded" }),
        installed("plugin.disabled", { status: "disabled", runtime_status: "stopped" }),
        installed("plugin.waiting", { runtime_status: "starting" }),
        installed("plugin.ready"),
      ],
      views: [
        view("plugin.quarantined"),
        view("plugin.crashed"),
        view("plugin.degraded"),
        view("plugin.ready"),
      ],
      documents: [],
    });
    const statuses = Object.fromEntries(entries.map((entry) => [entry.pluginId, entry.status]));

    expect(statuses).toMatchObject({
      "plugin.quarantined": "quarantined",
      "plugin.crashed": "crashed",
      "plugin.degraded": "degraded",
      "plugin.disabled": "disabled",
      "plugin.waiting": "waiting",
      "plugin.ready": "ready",
    });
  });

  it("selects a valid requested plugin and otherwise falls back deterministically to a relevant entry", () => {
    const entries = buildAssistantCatalog({
      installedPlugins: [
        installed("plugin.alpha", { name: "Alpha", runtime_status: "starting" }),
        installed("plugin.zulu", { name: "Zulu" }),
      ],
      views: [view("plugin.zulu")],
      documents: [],
    });

    expect(selectAssistantPluginId(entries, "plugin.alpha")).toBe("plugin.alpha");
    expect(selectAssistantPluginId(entries, "plugin.missing")).toBe("plugin.zulu");
    expect(selectAssistantPluginId(entries, null)).toBe("plugin.zulu");
    expect(selectAssistantPluginId([], "plugin.alpha")).toBeNull();
  });

  it("isolates field values by media session and composite view identity", () => {
    const courseKey = makeAssistantViewKey("media-1", "plugin.course", "panel", "main");
    const otherKey = makeAssistantViewKey("media-1", "plugin.other", "panel", "main");
    let values = setAssistantViewInput({}, courseKey, "language", "zh-CN");
    values = setAssistantViewInput(values, otherKey, "language", "en");
    values = setAssistantViewInput(values, courseKey, "detail", "outline");

    expect(values[courseKey]).toEqual({ language: "zh-CN", detail: "outline" });
    expect(values[otherKey]).toEqual({ language: "en" });
  });

  it("clears only values belonging to the old media session", () => {
    const oldKey = makeAssistantViewKey("media-old", "plugin.course", "panel", "main");
    const nextKey = makeAssistantViewKey("media-next", "plugin.course", "panel", "main");
    const values = {
      [oldKey]: { language: "zh-CN" },
      [nextKey]: { language: "en" },
    };

    expect(clearAssistantSessionInputs(values, "media-old")).toEqual({
      [nextKey]: { language: "en" },
    });
  });

  it("filters documents by trusted plugin identity and drops plugin-provided URLs", () => {
    const unsafe = {
      ...document("plugin.course", "../doc"),
      download_url: "javascript:alert(1)",
      markdown_path: "file:///etc/passwd",
    } as PluginDocumentSummary & Record<string, unknown>;
    const filtered = filterAssistantDocuments(
      [unsafe, document("plugin.other", "other")],
      "plugin.course",
    );

    expect(filtered).toHaveLength(1);
    expect(filtered[0]?.plugin_id).toBe("plugin.course");
    expect(JSON.stringify(filtered)).not.toContain("javascript:");
    expect(JSON.stringify(filtered)).not.toContain("file://");
  });

  it("normalizes URL selections, keeps a valid request, then prefers the newest running session", () => {
    const sessions = [
      session("history-new", "completed", "2026-08-29T03:00:00Z"),
      session("running-old", "running", "2026-08-29T01:00:00Z"),
      session("running-new", "running", "2026-08-29T02:00:00Z"),
    ];

    expect(normalizeAssistantQuerySelection(" running-old ")).toBe("running-old");
    expect(normalizeAssistantQuerySelection("../../bad?url")).toBeNull();
    expect(selectAssistantSessionId(sessions, "history-new")).toBe("history-new");
    expect(selectAssistantSessionId(sessions, "missing")).toBe("running-new");
  });

  it("falls back to latest history and rejects disposed or stale generations", () => {
    const sessions = [
      session("older", "failed", "2026-08-29T01:00:00Z"),
      session("newer", "completed", "2026-08-29T02:00:00Z"),
    ];

    expect(selectAssistantSessionId(sessions, null)).toBe("newer");
    expect(selectAssistantSessionId([], "missing")).toBeNull();
    expect(isAssistantGenerationActive(4, 4, false)).toBe(true);
    expect(isAssistantGenerationActive(4, 5, false)).toBe(false);
    expect(isAssistantGenerationActive(4, 4, true)).toBe(false);
  });

  it("summarizes Room assistant health and builds the exact workspace URL", () => {
    const entries = buildAssistantCatalog({
      installedPlugins: [
        installed("plugin.ready"),
        installed("plugin.degraded", { runtime_status: "degraded" }),
        installed("plugin.crashed", { runtime_status: "crashed" }),
      ],
      views: [view("plugin.ready"), view("plugin.degraded")],
      documents: [
        document("plugin.ready", "older"),
        {
          ...document("plugin.degraded", "newer"),
          created_at: "2026-08-29T04:00:00Z",
        },
      ],
    });
    const summary = summarizeRoomAssistants(
      entries,
      "session with spaces",
      "2026-08-29T03:00:00Z",
    );

    expect(summary.connectedCount).toBe(2);
    expect(summary.errorCount).toBe(2);
    expect(summary.latestUpdateAt).toBe("2026-08-29T04:00:00Z");
    expect(summary.href).toBe("/assistants?session=session%20with%20spaces");
    expect(summarizeRoomAssistants([], null, null).href).toBe("/assistants");
  });

  it("preserves an explicit uninstalled plugin deep link while defaulting ordinary visits", () => {
    const entries = buildAssistantCatalog({
      installedPlugins: [installed("plugin.ready")],
      views: [view("plugin.ready")],
      documents: [],
    });

    expect(selectAssistantPagePluginId([], "plugin.deep", "plugin.deep")).toBe("plugin.deep");
    expect(selectAssistantPagePluginId(entries, "plugin.deep", "plugin.deep")).toBe("plugin.deep");
    expect(selectAssistantPagePluginId(entries, null, null)).toBe("plugin.ready");
  });

  it("rejects stale overlapping requests and prevents unsafe fallback views from executing", () => {
    expect(isAssistantRequestCurrent(2, 2, 4, 4, false)).toBe(true);
    expect(isAssistantRequestCurrent(1, 2, 4, 4, false)).toBe(false);
    expect(isAssistantRequestCurrent(2, 2, 4, 5, false)).toBe(false);
    const parsedView = {
      ...view("plugin.ready"),
      view: {
        schema_version: 1,
        surface: "panel",
        view_id: "main",
        view_version: 1,
        root: { id: "message", type: "text", text: "safe previous" },
        actions: [],
      },
      safe: false,
    } as ParsedPluginViewEnvelope;
    expect(canExecuteAssistantView(parsedView)).toBe(false);
    expect(canExecuteAssistantView({ ...parsedView, safe: true })).toBe(true);
  });
});
