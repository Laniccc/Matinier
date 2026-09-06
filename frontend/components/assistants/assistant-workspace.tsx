"use client";

import { useEffect, useMemo, useState } from "react";

import { listSessions } from "@/lib/api";
import {
  buildAssistantCatalog,
  normalizeAssistantQuerySelection,
  selectAssistantPagePluginId,
  selectAssistantSessionId,
} from "@/lib/assistant-workspace-state";
import { useMediaAssistantWorkspace } from "@/components/plugins/use-media-assistant-workspace";
import { AssistantCatalog } from "./assistant-catalog";
import { AssistantDetail } from "./assistant-detail";
import { AssistantSessionSelector } from "./assistant-session-selector";
import type { Session } from "@/types/session";


function replaceSelection(sessionId: string | null, pluginId: string | null) {
  const url = new URL(window.location.href);
  if (sessionId) url.searchParams.set("session", sessionId);
  else url.searchParams.delete("session");
  if (pluginId) url.searchParams.set("plugin", pluginId);
  else url.searchParams.delete("plugin");
  window.history.replaceState(window.history.state, "", url);
}

export function AssistantWorkspace({
  initialSessionId,
  initialPluginId,
}: {
  initialSessionId: string | null;
  initialPluginId: string | null;
}) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(true);
  const [sessionsError, setSessionsError] = useState<string | null>(null);
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [selectedPluginId, setSelectedPluginId] = useState<string | null>(
    normalizeAssistantQuerySelection(initialPluginId),
  );
  const [requestedPluginId, setRequestedPluginId] = useState<string | null>(
    normalizeAssistantQuerySelection(initialPluginId),
  );
  const workspace = useMediaAssistantWorkspace(selectedSessionId);
  const catalog = useMemo(
    () =>
      buildAssistantCatalog({
        installedPlugins: workspace.plugins,
        views: workspace.views,
        documents: workspace.documents,
        historySources: workspace.historySources,
      }),
    [workspace.documents, workspace.plugins, workspace.views, workspace.historySources],
  );

  useEffect(() => {
    let disposed = false;
    const load = async () => {
      try {
        const nextSessions = await listSessions();
        if (disposed) return;
        setSessions(nextSessions);
        setSelectedSessionId(
          selectAssistantSessionId(nextSessions, initialSessionId),
        );
        setSessionsError(null);
      } catch (caught) {
        if (!disposed) {
          setSessionsError(
            caught instanceof Error ? caught.message : "字幕 Session 加载失败",
          );
        }
      } finally {
        if (!disposed) setSessionsLoading(false);
      }
    };
    void load();
    return () => {
      disposed = true;
    };
  }, [initialSessionId]);

  useEffect(() => {
    const nextPluginId = selectAssistantPagePluginId(
      catalog,
      selectedPluginId,
      requestedPluginId,
    );
    if (nextPluginId !== selectedPluginId) setSelectedPluginId(nextPluginId);
  }, [catalog, requestedPluginId, selectedPluginId]);

  useEffect(() => {
    if (!sessionsLoading) replaceSelection(selectedSessionId, selectedPluginId);
  }, [selectedPluginId, selectedSessionId, sessionsLoading]);

  const selectedEntry =
    catalog.find((entry) => entry.pluginId === selectedPluginId) ?? null;

  return (
    <main className="assistantWorkspaceShell">
      <header className="assistantWorkspaceHero">
        <div>
          <p className="eyebrow">Media assistants</p>
          <h1>助手工作区</h1>
          <p>按字幕 Session 查看多个插件助手的实时视图与可信交付文档。</p>
        </div>
        <nav aria-label="工作区导航">
          <a href="/">直播控制台</a>
          <a href="/plugins">插件管理</a>
        </nav>
      </header>

      <section className="studioPanel assistantSessionBar">
        <AssistantSessionSelector
          sessions={sessions}
          selectedSessionId={selectedSessionId}
          onSelect={(sessionId) => {
            setSelectedSessionId(sessionId);
            setSelectedPluginId(null);
            setRequestedPluginId(null);
          }}
        />
        {workspace.mediaSessionId ? <code>{workspace.mediaSessionId}</code> : null}
      </section>

      {sessionsLoading ? <p className="assistantPageStatus">正在加载字幕 Session…</p> : null}
      {sessionsError ? <p className="pluginInlineError" role="alert">{sessionsError}</p> : null}
      {!sessionsLoading && sessions.length === 0 && sessionsError === null ? (
        <section className="studioPanel assistantPageEmpty">
          <h2>还没有字幕 Session</h2>
          <p>先在直播控制台创建并运行一次字幕任务，再回到这里加载助手。</p>
          <a href="/">打开直播控制台</a>
        </section>
      ) : null}

      {selectedSessionId !== null ? (
        <div className="assistantWorkspaceGrid">
          <AssistantCatalog
            entries={catalog}
            selectedPluginId={selectedPluginId}
            onSelect={(pluginId) => {
              setRequestedPluginId(null);
              setSelectedPluginId(pluginId);
            }}
          />
          {workspace.loading ? (
            <section className="studioPanel assistantPageEmpty">
              <p>正在连接当前 MediaSession…</p>
            </section>
          ) : workspace.errors.connection && catalog.length === 0 ? (
            <section className="studioPanel assistantPageEmpty">
              <p className="pluginInlineError" role="alert">{workspace.errors.connection}</p>
            </section>
          ) : workspace.frameworkEnabled === false && catalog.length === 0 ? (
            <section className="studioPanel assistantPageEmpty">
              <h2>插件框架已关闭</h2>
              <p>当前 Host 配置未启用插件框架，字幕主链仍可独立运行。</p>
              <a href="/plugins">检查插件配置</a>
            </section>
          ) : workspace.errors.plugins && catalog.length === 0 ? (
            <section className="studioPanel assistantPageEmpty">
              <h2>插件框架暂不可用</h2>
              <p className="pluginInlineError" role="alert">{workspace.errors.plugins}</p>
              <a href="/plugins">检查插件配置</a>
            </section>
          ) : (
            <AssistantDetail
              hostActions={workspace.hostActions} hostError={workspace.hostError}
              approvals={workspace.approvals} approvalBusyIds={workspace.approvalBusyIds}
              onResolveApproval={(item, decision) => void workspace.resolveApproval(item, decision)}
              mediaSessionId={workspace.mediaSessionId}
              entry={selectedEntry}
              requestedPluginId={selectedEntry === null ? selectedPluginId : null}
              parsedViews={workspace.views}
              inputs={workspace.inputs}
              busyViewKeys={workspace.busyViewKeys}
              viewError={workspace.errors.views}
              documentError={workspace.errors.documents}
              commandError={
                selectedPluginId
                  ? workspace.commandErrors[selectedPluginId] ?? null
                  : null
              }
              onValueChange={workspace.setViewInput}
              onAction={(view, actionId) => void workspace.runAction(view, actionId)}
            />
          )}
        </div>
      ) : null}
    </main>
  );
}
