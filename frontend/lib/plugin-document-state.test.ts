import { describe, expect, it } from "vitest";

import { getPluginDocumentExportUrl } from "./api";
import {
  createPluginDocumentState,
  groupPluginDocuments,
  reducePluginDocumentState,
} from "./plugin-document-state";
import type { PluginDocumentSummary } from "@/types/plugins";


function document(
  documentId: string,
  version: number,
  overrides: Partial<PluginDocumentSummary> = {},
): PluginDocumentSummary {
  return {
    document_id: documentId,
    plugin_id: "com.matinier.course-organizer",
    plugin_version: "1.0.0",
    media_session_id: "media-1",
    source_package_id: `package-${version}`,
    source_package_version: version,
    source_package_hash: `${version}`.repeat(64).slice(0, 64),
    identity_key: "course-notes:zh-CN",
    document_version: version,
    schema_name: "matinier.course-notes",
    schema_version: "1.0",
    language: "zh-CN",
    trigger: "manual",
    completeness: "interim",
    status: "published",
    content_hash: `${version + 1}`.repeat(64).slice(0, 64),
    created_at: `2026-08-29T00:00:0${version}Z`,
    ...overrides,
  };
}


describe("plugin document state", () => {
  it("groups by plugin, identity, and language while preserving interim and complete versions", () => {
    const documents = [
      document("zh-interim", 1),
      document("zh-complete", 2, {
        trigger: "session_completed",
        completeness: "complete",
      }),
      document("en-interim", 1, {
        identity_key: "course-notes:en",
        language: "en",
      }),
    ];
    const groups = groupPluginDocuments(documents);

    expect(groups).toHaveLength(2);
    const chinese = groups.find((item) => item.language === "zh-CN");
    expect(chinese?.versions.map((item) => item.document_id)).toEqual([
      "zh-complete",
      "zh-interim",
    ]);
    expect(chinese?.newest.document_id).toBe("zh-complete");
    expect(chinese?.latestInterim?.document_id).toBe("zh-interim");
    expect(chinese?.latestComplete?.document_id).toBe("zh-complete");
  });

  it("represents loading, loaded-empty, retained refresh, and sanitized errors", () => {
    let state = createPluginDocumentState();
    state = reducePluginDocumentState(state, { type: "loading" });
    expect(state.status).toBe("loading");

    state = reducePluginDocumentState(state, { type: "loaded", documents: [] });
    expect(state.status).toBe("ready");
    expect(state.groups).toEqual([]);

    state = reducePluginDocumentState(state, {
      type: "loaded",
      documents: [document("doc-1", 1)],
    });
    state = reducePluginDocumentState(state, { type: "refreshing" });
    expect(state.status).toBe("refreshing");
    expect(state.groups[0]?.newest.document_id).toBe("doc-1");

    state = reducePluginDocumentState(state, {
      type: "failed",
      message: "文档历史刷新失败",
    });
    expect(state.status).toBe("error");
    expect(state.error).toBe("文档历史刷新失败");
    expect(state.groups[0]?.newest.document_id).toBe("doc-1");
  });

  it("drops plugin-provided paths and builds exports only from the trusted API route", () => {
    const untrusted = {
      ...document("../doc?redirect=https://evil.example", 1),
      download_url: "javascript:alert(1)",
      markdown_path: "file:///etc/passwd",
    } as PluginDocumentSummary & Record<string, unknown>;
    const [group] = groupPluginDocuments([untrusted]);
    expect(JSON.stringify(group)).not.toContain("javascript:");
    expect(JSON.stringify(group)).not.toContain("file://");

    const markdown = getPluginDocumentExportUrl(
      group.newest.document_id,
      "markdown",
    );
    const json = getPluginDocumentExportUrl(group.newest.document_id, "json");
    expect(markdown).toBe(
      "http://localhost:8000/api/plugin-documents/..%2Fdoc%3Fredirect%3Dhttps%3A%2F%2Fevil.example/export?format=markdown",
    );
    expect(json.endsWith("/export?format=json")).toBe(true);
    expect(markdown).not.toContain("javascript:");
  });
});
