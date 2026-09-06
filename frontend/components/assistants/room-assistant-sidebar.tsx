"use client";

import { useMemo, useState } from "react";
import { buildAssistantCatalog, selectAssistantPluginId, summarizeRoomAssistants } from "@/lib/assistant-workspace-state";
import { useMediaAssistantWorkspace } from "@/components/plugins/use-media-assistant-workspace";
import { AssistantCatalog } from "./assistant-catalog";
import { AssistantDetail } from "./assistant-detail";

export function RoomAssistantSidebar({ legacySessionId }: { legacySessionId: string | null }) {
  // One connection owner for the current run, independent of which sidebar panel is visible.
  const workspace = useMediaAssistantWorkspace(legacySessionId);
  const [requestedPluginId, setRequestedPluginId] = useState<string | null>(null);
  const catalog = useMemo(() => buildAssistantCatalog({
    installedPlugins: workspace.plugins, views: workspace.views, documents: workspace.documents, historySources: workspace.historySources,
  }), [workspace.plugins, workspace.views, workspace.documents, workspace.historySources]);
  const selectedPluginId = selectAssistantPluginId(catalog, requestedPluginId);
  const selectedEntry = catalog.find((entry) => entry.pluginId === selectedPluginId) ?? null;
  const summary = summarizeRoomAssistants(catalog, legacySessionId, workspace.latestViewUpdateAt);
  const connectionError = workspace.errors.connection ?? workspace.errors.health ?? workspace.errors.plugins;

  return (
    <div className="roomAssistantSidebar">
      <div className="roomAssistantContext">
        <span className="label">Current caption session</span>
        <strong>{legacySessionId ? "跟随当前字幕任务" : "尚未连接字幕任务"}</strong>
        {legacySessionId ? <code title={legacySessionId}>{legacySessionId}</code> : null}
        <p>切换助手不影响音频采集和实时字幕。</p>
        <nav aria-label="助手辅助页面">
          <a href={summary.href} target="_blank" rel="noopener noreferrer">历史工作区 ↗</a>
          <a href="/plugins" target="_blank" rel="noopener noreferrer">插件管理 ↗</a>
        </nav>
      </div>
      {legacySessionId === null ? (
        <p className="assistantNotice">启动或选择字幕任务后，助手会在这里接收实时内容。</p>
      ) : workspace.loading ? (
        <p className="assistantNotice" role="status">正在连接当前助手状态…</p>
      ) : workspace.frameworkEnabled === false && catalog.length === 0 ? (
        <p className="assistantNotice" role="status">插件框架已关闭，字幕仍可独立运行。可在插件管理中检查配置。</p>
      ) : (
        <>
          <div className="roomAssistantSummary">
            <span>{summary.connectedCount} 个已连接 · {summary.errorCount} 个异常</span>
            {workspace.latestViewUpdateAt ? <time dateTime={workspace.latestViewUpdateAt}>
              更新于 {new Date(workspace.latestViewUpdateAt).toLocaleTimeString()}
            </time> : null}
          </div>
          {connectionError ? <p className="pluginInlineError" role="alert">{connectionError}</p> : null}
          <AssistantCatalog entries={catalog} selectedPluginId={selectedPluginId} onSelect={setRequestedPluginId} navigationTarget="_blank" />
          <AssistantDetail mediaSessionId={workspace.mediaSessionId} entry={selectedEntry} requestedPluginId={null}
            hostActions={workspace.hostActions} hostError={workspace.hostError}
            approvals={workspace.approvals} approvalBusyIds={workspace.approvalBusyIds}
            onResolveApproval={(item, decision) => void workspace.resolveApproval(item, decision)}
            parsedViews={workspace.views} inputs={workspace.inputs} busyViewKeys={workspace.busyViewKeys}
            viewError={workspace.errors.views} documentError={workspace.errors.documents}
            commandError={selectedPluginId ? workspace.commandErrors[selectedPluginId] ?? null : null}
            onValueChange={workspace.setViewInput} onAction={(view, actionId) => void workspace.runAction(view, actionId)}
            navigationTarget="_blank" />
        </>
      )}
    </div>
  );
}
