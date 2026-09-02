"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { executePluginCommand, listPluginViews, resolveMediaSession } from "@/lib/api";
import { parsePluginUIView } from "@/lib/plugin-ui-schema";
import type { PluginUIViewDocument } from "@/types/plugin-ui";
import type { ParsedPluginViewEnvelope } from "@/types/plugins";
import { PluginComponent } from "./plugin-component";
import { PluginDocumentShelf } from "./plugin-document-shelf";
import { canExecuteAssistantView } from "@/lib/assistant-workspace-state";


export function PluginViewCard({
  view,
  values,
  busy,
  onValueChange,
  onAction,
}: {
  view: ParsedPluginViewEnvelope;
  values: Record<string, unknown>;
  busy: boolean;
  onValueChange: (name: string, value: unknown) => void;
  onAction: (actionId: string) => void;
}) {
  return (
    <article className="pluginView">
      <header>
        <strong>{view.view_id}</strong>
        <span>{view.safe ? `v${view.view_version}` : "已安全降级"}</span>
      </header>
      <PluginComponent
        component={view.view.root}
        values={values}
        onValueChange={onValueChange}
        onAction={onAction}
        disabled={busy || !canExecuteAssistantView(view)}
      />
    </article>
  );
}

export function PluginSurface({ legacySessionId }: { legacySessionId: string | null }) {
  const [mediaSessionId, setMediaSessionId] = useState<string | null>(null);
  const [views, setViews] = useState<ParsedPluginViewEnvelope[]>([]);
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const previousViews = useRef(new Map<string, PluginUIViewDocument>());

  const refresh = useCallback(async (targetMediaSessionId: string) => {
    const envelopes = await listPluginViews(targetMediaSessionId);
    const next = envelopes.map((envelope) => {
      const key = `${envelope.plugin_id}:${envelope.surface}:${envelope.view_id}`;
      const previous = previousViews.current.get(key);
      if (previous && previous.view_version === envelope.view_version) {
        return { ...envelope, view: previous, safe: true };
      }
      const parsed = parsePluginUIView(envelope.view, {
        allowedCommands: new Set(envelope.allowed_commands),
        previous,
      });
      if (parsed.ok) previousViews.current.set(key, parsed.view);
      return { ...envelope, view: parsed.view, safe: parsed.ok };
    });
    setViews(next);
  }, []);

  useEffect(() => {
    previousViews.current.clear();
    setViews([]);
    setMediaSessionId(null);
    setValues({});
    setError(null);
    if (legacySessionId === null) return;
    let disposed = false;
    let timer: number | null = null;
    const connect = async () => {
      try {
        const bridge = await resolveMediaSession(legacySessionId);
        if (disposed) return;
        setMediaSessionId(bridge.media_session_id);
        await refresh(bridge.media_session_id);
        if (!disposed) {
          timer = window.setInterval(() => {
            void refresh(bridge.media_session_id).catch((caught) => {
              setError(caught instanceof Error ? caught.message : "插件视图刷新失败");
            });
          }, 2_000);
        }
      } catch (caught) {
        if (!disposed) setError(caught instanceof Error ? caught.message : "插件会话连接失败");
      }
    };
    void connect();
    return () => {
      disposed = true;
      if (timer !== null) window.clearInterval(timer);
    };
  }, [legacySessionId, refresh]);

  const runAction = async (view: ParsedPluginViewEnvelope, actionId: string) => {
    if (mediaSessionId === null) return;
    const actionKey = `${view.plugin_id}:${actionId}`;
    setBusyAction(actionKey);
    setError(null);
    try {
      await executePluginCommand(mediaSessionId, {
        plugin_id: view.plugin_id,
        plugin_version: view.plugin_version,
        session_scope: view.session_scope,
        surface: view.surface,
        view_id: view.view_id,
        expected_view_version: view.view_version,
        action_id: actionId,
        values,
      });
      await refresh(mediaSessionId);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "插件命令执行失败");
    } finally {
      setBusyAction(null);
    }
  };

  return (
    <section className="studioPanel pluginSurfacePanel">
      <div className="panelHeading">
        <div><span className="label">Plugin surfaces</span><h2>观看助手</h2></div>
        <a href="/plugins" className="pluginManageLink">管理</a>
      </div>
      {legacySessionId === null ? <p className="emptyState">启动或选择字幕任务后加载兼容助手。</p> : null}
      {error ? <p className="pluginInlineError" role="alert">{error}</p> : null}
      {legacySessionId !== null && views.length === 0 && error === null ? <p className="emptyState">当前没有插件视图。</p> : null}
      <div className="pluginViews">
        {views.map((view) => (
          <PluginViewCard
              key={`${view.plugin_id}:${view.surface}:${view.view_id}`}
              view={view}
              values={values}
              onValueChange={(name, value) => setValues((current) => ({ ...current, [name]: value }))}
              onAction={(actionId) => void runAction(view, actionId)}
              busy={busyAction?.startsWith(`${view.plugin_id}:`) ?? false}
          />
        ))}
      </div>
      {mediaSessionId !== null ? (
        <PluginDocumentShelf
          key={mediaSessionId}
          mediaSessionId={mediaSessionId}
        />
      ) : null}
    </section>
  );
}
