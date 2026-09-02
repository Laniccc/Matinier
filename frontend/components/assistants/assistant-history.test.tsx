// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";
import { buildAssistantCatalog } from "@/lib/assistant-workspace-state";
import { AssistantDetail } from "./assistant-detail";

vi.mock("@/components/plugins/host-history-panel", () => ({ HostHistoryPanel: () => <p>原始历史记录</p> }));
vi.mock("@/components/plugins/plugin-document-shelf", () => ({ PluginDocumentShelf: () => null }));
const source = { source_kind: "host_history" as const, plugin_id: "com.matinier.meeting-assistant", name: "会议助手", media_session_id: "media", legacy_session_id: "legacy" };
describe("generic Host history", () => {
  it("merges and deduplicates history without an installation, view or document", () => {
    const entries = buildAssistantCatalog({ installedPlugins: [], views: [], documents: [], historySources: [source, source] });
    expect(entries).toHaveLength(1);
    expect(entries[0]).toMatchObject({ name: "会议助手", status: "historical", historySources: [source] });
    expect(entries.some(entry => entry.pluginId === "unknown-plugin")).toBe(false);
  });
  it("keeps uninstalled history readable with only Host-declared cancel controls", async () => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    const node = document.createElement("div"); const root = createRoot(node);
    const entry = buildAssistantCatalog({ installedPlugins: [], views: [], documents: [], historySources: [source] })[0];
    await act(async () => root.render(<AssistantDetail mediaSessionId="media" entry={entry} requestedPluginId={null} parsedViews={[]}
      inputs={{}} busyViewKeys={new Set()} viewError={null} documentError={null} commandError={null} onValueChange={() => {}} onAction={() => {}}
      hostActions={[{ ...source, plugin_version: null, view_version: null, authority_epoch: 0, analysis_epoch: 0, actions: [{ id: "cancel", action: "meeting.cancel", label: "取消执行", enabled: false, reason: "没有可取消执行", fields: [], fixed_arguments: {} }] }]} />));
    expect(node.textContent).toContain("原始历史记录"); expect(node.textContent).toContain("取消执行");
    expect(node.textContent).not.toContain("执行到 Linear"); expect(node.textContent).not.toContain("补充输入");
    await act(async () => root.unmount()); vi.unstubAllGlobals();
  });
});
