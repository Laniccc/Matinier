"use client";
import { useState } from "react";

import { makeAssistantViewKey } from "@/lib/assistant-workspace-state";
import { PluginDocumentShelf } from "@/components/plugins/plugin-document-shelf";
import { PluginViewCard } from "@/components/plugins/plugin-surface";
import { HostActionPanel } from "@/components/plugins/host-action-panel";
import { HostHistoryPanel } from "@/components/plugins/host-history-panel";
import type { HostActionDescriptor, HostActionSelection } from "@/types/host-actions";
import { hostContextKey } from "@/lib/host-action-state";
import type {
  AssistantCatalogEntry,
  AssistantViewInputState,
  ParsedPluginViewEnvelope,
} from "@/types/plugins";


export function AssistantDetail({
  mediaSessionId,
  entry,
  requestedPluginId,
  parsedViews,
  inputs,
  busyViewKeys,
  viewError,
  documentError,
  commandError,
  onValueChange,
  onAction,
  navigationTarget,
  hostActions = [],
  hostError,
}: {
  mediaSessionId: string | null;
  entry: AssistantCatalogEntry | null;
  requestedPluginId: string | null;
  parsedViews: ParsedPluginViewEnvelope[];
  inputs: AssistantViewInputState;
  busyViewKeys: Set<string>;
  viewError: string | null;
  documentError: string | null;
  commandError: string | null;
  onValueChange: (
    view: ParsedPluginViewEnvelope,
    name: string,
    value: unknown,
  ) => void;
  onAction: (view: ParsedPluginViewEnvelope, actionId: string) => void;
  navigationTarget?: "_blank";
  hostActions?: HostActionDescriptor[];
  hostError?: string | null;
}) {
  const [selection, setSelection] = useState<HostActionSelection | null>(null);
  if (entry === null) {
    return (
      <section className="studioPanel assistantDetail assistantDetailEmpty">
        <h2>{requestedPluginId ? "插件尚未安装" : "选择一个助手"}</h2>
        <p>
          {requestedPluginId
            ? `未找到 ${requestedPluginId} 的安装记录、视图或历史文档。`
            : "安装并启用插件后，它会在这里接收当前字幕 Session 的事件。"}
        </p>
        <a href="/plugins" target={navigationTarget} rel={navigationTarget ? "noopener noreferrer" : undefined}>打开插件管理</a>
      </section>
    );
  }

  const selectedViews = parsedViews.filter(
    (view) => view.plugin_id === entry.pluginId,
  );
  const degraded = ["degraded", "crashed", "quarantined"].includes(entry.status);

  return (
    <section className="studioPanel assistantDetail">
      <header className="assistantDetailHeader">
        <div>
          <span className="label">Selected assistant</span>
          <h2>{entry.name}</h2>
          <code>{entry.pluginId}</code>
        </div>
        <span className={`assistantStatus assistantStatus-${entry.status}`}>
          {entry.status}
        </span>
      </header>

      {entry.installed === null && entry.documents.length > 0 ? (
        <p className="assistantNotice">该插件当前未安装；下方保留不可变历史文档。</p>
      ) : null}
      {entry.status === "disabled" ? (
        <p className="assistantNotice">插件已安装但未启用。<a href="/plugins" target={navigationTarget} rel={navigationTarget ? "noopener noreferrer" : undefined}>前往管理</a></p>
      ) : null}
      {degraded ? (
        <p className="pluginInlineError" role="alert">
          插件运行状态为 {entry.status}，已有内容仍可查看，其他助手不受影响。
        </p>
      ) : null}
      {commandError ? <p className="pluginInlineError" role="alert">{commandError}</p> : null}
      {viewError ? <p className="pluginInlineError" role="alert">视图刷新失败：{viewError}</p> : null}
      {hostError ? <p className="pluginInlineError" role="alert">主程序操作/历史刷新失败：{hostError}</p> : null}
      {hostActions.filter(item => item.plugin_id === entry.pluginId && item.media_session_id === mediaSessionId).map(item =>
        <HostActionPanel key={`${item.media_session_id}:${item.plugin_id}:${item.source_kind}`} descriptor={item} selection={selection} />)}

      <div className="assistantViewStack">
        {selectedViews.map((view) => {
          const viewKey = mediaSessionId
            ? makeAssistantViewKey(
                mediaSessionId,
                view.plugin_id,
                view.surface,
                view.view_id,
              )
            : "";
          return (
            <PluginViewCard
              key={`${view.plugin_id}:${view.surface}:${view.view_id}`}
              view={view}
              values={inputs[viewKey] ?? {}}
              busy={busyViewKeys.has(viewKey) || entry.installed?.status !== "enabled"}
              onValueChange={(name, value) => onValueChange(view, name, value)}
              onAction={(actionId) => {
                const command = view.view.actions.find(item => item.id === actionId)?.command;
                const owner = hostActions.find(item => item.plugin_id === view.plugin_id && item.media_session_id === mediaSessionId && item.plugin_version === view.plugin_version);
                const control = owner?.actions.find(item => item.trigger_command === command);
                if (owner && control) {
                  setSelection({ contextKey: hostContextKey(owner), actionId: control.id, values: inputs[viewKey] ?? {}, serial: crypto.randomUUID() });
                } else onAction(view, actionId);
              }}
            />
          );
        })}
      </div>
      {(entry.historySources ?? []).map(source => <HostHistoryPanel key={`${source.plugin_id}:${source.media_session_id}`} source={source} />)}

      {selectedViews.length === 0 && entry.status === "waiting" ? (
        <p className="emptyState">正在等待插件为当前 Session 发布视图。</p>
      ) : null}

      {mediaSessionId !== null ? (
        <PluginDocumentShelf
          mediaSessionId={mediaSessionId}
          pluginId={entry.pluginId}
          documents={entry.documents}
          externalError={documentError}
        />
      ) : null}
    </section>
  );
}
