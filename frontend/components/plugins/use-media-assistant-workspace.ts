"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  executePluginCommand,
  getPluginFrameworkHealth,
  listPluginDocuments,
  listPlugins,
  listPluginViews,
  resolveMediaSession,
  getAssistantHistorySources,
  getHostActionDescriptors,
} from "@/lib/api";
import {
  clearAssistantSessionInputs,
  canExecuteAssistantView,
  isAssistantGenerationActive,
  isAssistantRequestCurrent,
  makeAssistantViewKey,
  setAssistantViewInput,
} from "@/lib/assistant-workspace-state";
import { parsePluginUIView } from "@/lib/plugin-ui-schema";
import type { PluginUIViewDocument } from "@/types/plugin-ui";
import type {
  AssistantViewInputState,
  InstalledPlugin,
  ParsedPluginViewEnvelope,
  PluginDocumentSummary,
  HostHistorySource,
} from "@/types/plugins";
import type { HostActionDescriptor } from "@/types/host-actions";


interface WorkspaceErrors {
  health: string | null;
  connection: string | null;
  plugins: string | null;
  views: string | null;
  documents: string | null;
}

const EMPTY_ERRORS: WorkspaceErrors = {
  health: null,
  connection: null,
  plugins: null,
  views: null,
  documents: null,
};

function errorMessage(value: unknown, fallback: string): string {
  return value instanceof Error ? value.message : fallback;
}

function viewSignature(
  views: Awaited<ReturnType<typeof listPluginViews>>,
): string {
  return views
    .map((view) =>
      [view.plugin_id, view.surface, view.view_id, view.view_version].join(":"),
    )
    .sort()
    .join("|");
}


export function useMediaAssistantWorkspace(legacySessionId: string | null) {
  const [mediaSessionId, setMediaSessionId] = useState<string | null>(null);
  const [plugins, setPlugins] = useState<InstalledPlugin[]>([]);
  const [views, setViews] = useState<ParsedPluginViewEnvelope[]>([]);
  const [documents, setDocuments] = useState<PluginDocumentSummary[]>([]);
  const [historySources, setHistorySources] = useState<HostHistorySource[]>([]);
  const [hostActions, setHostActions] = useState<HostActionDescriptor[]>([]);
  const [hostError, setHostError] = useState<string | null>(null);
  const [inputs, setInputs] = useState<AssistantViewInputState>({});
  const [busyViewKeys, setBusyViewKeys] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(false);
  const [frameworkEnabled, setFrameworkEnabled] = useState<boolean | null>(null);
  const [latestViewUpdateAt, setLatestViewUpdateAt] = useState<string | null>(null);
  const [errors, setErrors] = useState<WorkspaceErrors>(EMPTY_ERRORS);
  const [commandErrors, setCommandErrors] = useState<Record<string, string>>({});
  const generationRef = useRef(0);
  const viewRequestRef = useRef(0);
  const mediaSessionRef = useRef<string | null>(null);
  const previousViews = useRef(new Map<string, PluginUIViewDocument>());
  const lastViewSignatureRef = useRef<string | null>(null);

  // Host history is a separate read-only source, including when the runtime is off.
  useEffect(() => {
    let disposed = false; let busy = false;
    setHistorySources([]); setHostActions([]); setHostError(null);
    if (!legacySessionId) return;
    async function refreshHost() {
      if (busy) return; busy = true;
      try {
        const sources = await getAssistantHistorySources(legacySessionId!);
        if (disposed) return;
        setHistorySources(sources);
        const target = mediaSessionId ?? sources[0]?.media_session_id;
        if (target) {
          const descriptors = await getHostActionDescriptors(target);
          if (disposed) return;
          setHostActions(descriptors);
          if (!mediaSessionId) setMediaSessionId(target);
        }
        setHostError(null);
      } catch (caught) {
        if (!disposed) { setHostActions([]); setHostError(errorMessage(caught, "主程序历史暂不可用")); }
      } finally { busy = false; }
    }
    void refreshHost();
    const timer = window.setInterval(() => void refreshHost(), 2000);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [legacySessionId, mediaSessionId]);

  const parseViews = useCallback(
    (targetMediaSessionId: string, envelopes: Awaited<ReturnType<typeof listPluginViews>>) =>
      envelopes.map((envelope): ParsedPluginViewEnvelope => {
        const key = makeAssistantViewKey(
          targetMediaSessionId,
          envelope.plugin_id,
          envelope.surface,
          envelope.view_id,
        );
        const previous = previousViews.current.get(key);
        if (previous && previous.view_version === envelope.view_version) {
          return { ...envelope, view: previous, safe: true };
        }
        const parsed = parsePluginUIView(envelope.view, {
          allowedCommands: new Set(envelope.allowed_commands),
          previous,
        });
        if (parsed.ok) {
          previousViews.current.set(key, parsed.view);
          return { ...envelope, view: parsed.view, safe: true };
        }
        return {
          ...envelope,
          view: previous ?? parsed.view,
          safe: false,
        };
      }),
    [],
  );

  useEffect(() => {
    const generation = ++generationRef.current;
    let disposed = false;
    let viewTimer: number | null = null;
    let documentTimer: number | null = null;
    let documentRequest = 0;

    viewRequestRef.current += 1;

    const staleMediaSessionId = mediaSessionRef.current;
    mediaSessionRef.current = null;
    if (staleMediaSessionId !== null) {
      setInputs((current) =>
        clearAssistantSessionInputs(current, staleMediaSessionId),
      );
      for (const key of previousViews.current.keys()) {
        if (key.startsWith(`${staleMediaSessionId}:`)) {
          previousViews.current.delete(key);
        }
      }
    }

    setMediaSessionId(null);
    setPlugins([]);
    setViews([]);
    setDocuments([]);
    setBusyViewKeys(new Set());
    setFrameworkEnabled(null);
    setLatestViewUpdateAt(null);
    lastViewSignatureRef.current = null;
    setCommandErrors({});
    setErrors(EMPTY_ERRORS);
    if (legacySessionId === null) {
      setLoading(false);
      return () => {
        disposed = true;
      };
    }
    setLoading(true);

    const active = () =>
      isAssistantGenerationActive(
        generation,
        generationRef.current,
        disposed,
      );

    const refreshViews = async (targetMediaSessionId: string) => {
      const request = ++viewRequestRef.current;
      try {
        const nextViews = await listPluginViews(targetMediaSessionId);
        if (
          !isAssistantRequestCurrent(
            request,
            viewRequestRef.current,
            generation,
            generationRef.current,
            disposed,
          )
        ) return;
        setViews(parseViews(targetMediaSessionId, nextViews));
        const signature = viewSignature(nextViews);
        if (signature !== lastViewSignatureRef.current) {
          lastViewSignatureRef.current = signature;
          setLatestViewUpdateAt(new Date().toISOString());
        }
        setErrors((current) => ({ ...current, views: null }));
      } catch (caught) {
        if (
          isAssistantRequestCurrent(
            request,
            viewRequestRef.current,
            generation,
            generationRef.current,
            disposed,
          )
        ) {
          setErrors((current) => ({
            ...current,
            views: errorMessage(caught, "插件视图刷新失败"),
          }));
        }
      }
    };

    const refreshDocumentsAndPlugins = async (targetMediaSessionId: string) => {
      const request = ++documentRequest;
      const [nextHealth, nextPlugins, nextDocuments] = await Promise.allSettled([
        getPluginFrameworkHealth(),
        listPlugins(),
        listPluginDocuments(targetMediaSessionId),
      ]);
      if (
        !isAssistantRequestCurrent(
          request,
          documentRequest,
          generation,
          generationRef.current,
          disposed,
        )
      ) return;
      if (nextHealth.status === "fulfilled") {
        setFrameworkEnabled(nextHealth.value.framework_enabled);
        setErrors((current) => ({ ...current, health: null }));
      } else {
        setErrors((current) => ({
          ...current,
          health: errorMessage(nextHealth.reason, "插件框架状态检查失败"),
        }));
      }
      if (nextPlugins.status === "fulfilled") {
        setPlugins(nextPlugins.value);
        setErrors((current) => ({ ...current, plugins: null }));
      } else {
        setErrors((current) => ({
          ...current,
          plugins: errorMessage(nextPlugins.reason, "插件框架不可用"),
        }));
      }
      if (nextDocuments.status === "fulfilled") {
        setDocuments(nextDocuments.value);
        setErrors((current) => ({ ...current, documents: null }));
      } else {
        setErrors((current) => ({
          ...current,
          documents: errorMessage(nextDocuments.reason, "助手文档刷新失败"),
        }));
      }
    };

    const connect = async () => {
      try {
        let initialHealthError: string | null = null;
        try {
          const initialHealth = await getPluginFrameworkHealth();
          if (!active()) return;
          setFrameworkEnabled(initialHealth.framework_enabled);
          setErrors((current) => ({ ...current, health: null }));
          if (!initialHealth.framework_enabled) {
            setLoading(false);
            return;
          }
        } catch (caught) {
          if (!active()) return;
          initialHealthError = errorMessage(
            caught,
            "插件框架状态检查失败",
          );
          setErrors((current) => ({
            ...current,
            health: initialHealthError,
          }));
        }
        const bridge = await resolveMediaSession(legacySessionId);
        if (!active()) return;
        mediaSessionRef.current = bridge.media_session_id;
        setMediaSessionId(bridge.media_session_id);

        const [nextPlugins, nextViews, nextDocuments] = await Promise.allSettled([
          listPlugins(),
          listPluginViews(bridge.media_session_id),
          listPluginDocuments(bridge.media_session_id),
        ]);
        if (!active()) return;
        if (nextPlugins.status === "fulfilled") setPlugins(nextPlugins.value);
        if (nextViews.status === "fulfilled") {
          setViews(parseViews(bridge.media_session_id, nextViews.value));
          lastViewSignatureRef.current = viewSignature(nextViews.value);
          if (nextViews.value.length > 0) {
            setLatestViewUpdateAt(new Date().toISOString());
          }
        }
        if (nextDocuments.status === "fulfilled") setDocuments(nextDocuments.value);
        setErrors({
          health: initialHealthError,
          connection: null,
          plugins:
            nextPlugins.status === "rejected"
              ? errorMessage(nextPlugins.reason, "插件框架不可用")
              : null,
          views:
            nextViews.status === "rejected"
              ? errorMessage(nextViews.reason, "插件视图加载失败")
              : null,
          documents:
            nextDocuments.status === "rejected"
              ? errorMessage(nextDocuments.reason, "助手文档加载失败")
              : null,
        });
        setLoading(false);
        viewTimer = window.setInterval(
          () => void refreshViews(bridge.media_session_id),
          2_000,
        );
        documentTimer = window.setInterval(
          () => void refreshDocumentsAndPlugins(bridge.media_session_id),
          4_000,
        );
      } catch (caught) {
        if (active()) {
          setLoading(false);
          setErrors((current) => ({
            ...current,
            connection: errorMessage(caught, "媒体会话连接失败"),
          }));
        }
      }
    };

    void connect();
    return () => {
      disposed = true;
      if (viewTimer !== null) window.clearInterval(viewTimer);
      if (documentTimer !== null) window.clearInterval(documentTimer);
    };
  }, [legacySessionId, parseViews]);

  const setViewInput = useCallback(
    (view: ParsedPluginViewEnvelope, name: string, value: unknown) => {
      if (mediaSessionId === null) return;
      const key = makeAssistantViewKey(
        mediaSessionId,
        view.plugin_id,
        view.surface,
        view.view_id,
      );
      setInputs((current) => setAssistantViewInput(current, key, name, value));
    },
    [mediaSessionId],
  );

  const runAction = useCallback(
    async (view: ParsedPluginViewEnvelope, actionId: string) => {
      if (mediaSessionId === null || !canExecuteAssistantView(view)) return;
      const actionGeneration = generationRef.current;
      const viewKey = makeAssistantViewKey(
        mediaSessionId,
        view.plugin_id,
        view.surface,
        view.view_id,
      );
      setBusyViewKeys((current) => new Set(current).add(viewKey));
      setCommandErrors((current) => {
        const next = { ...current };
        delete next[view.plugin_id];
        return next;
      });
      try {
        try {
          await executePluginCommand(mediaSessionId, {
            plugin_id: view.plugin_id,
            plugin_version: view.plugin_version,
            session_scope: view.session_scope,
            surface: view.surface,
            view_id: view.view_id,
            expected_view_version: view.view_version,
            action_id: actionId,
            values: inputs[viewKey] ?? {},
          });
        } catch (caught) {
          if (
            generationRef.current === actionGeneration &&
            mediaSessionRef.current === mediaSessionId
          ) {
            setCommandErrors((current) => ({
              ...current,
              [view.plugin_id]: errorMessage(caught, "插件命令执行失败"),
            }));
          }
          return;
        }

        const request = ++viewRequestRef.current;
        try {
          const nextViews = await listPluginViews(mediaSessionId);
          if (
            request === viewRequestRef.current &&
            generationRef.current === actionGeneration &&
            mediaSessionRef.current === mediaSessionId
          ) {
            setViews(parseViews(mediaSessionId, nextViews));
            setErrors((current) => ({ ...current, views: null }));
          }
        } catch (caught) {
          if (
            request === viewRequestRef.current &&
            generationRef.current === actionGeneration &&
            mediaSessionRef.current === mediaSessionId
          ) {
            setErrors((current) => ({
              ...current,
              views: errorMessage(caught, "插件视图刷新失败"),
            }));
          }
        }
      } finally {
        setBusyViewKeys((current) => {
          const next = new Set(current);
          next.delete(viewKey);
          return next;
        });
      }
    },
    [inputs, mediaSessionId, parseViews],
  );

  return {
    mediaSessionId,
    plugins,
    views,
    documents,
    historySources,
    hostActions,
    hostError,
    inputs,
    busyViewKeys,
    loading,
    frameworkEnabled,
    latestViewUpdateAt,
    errors,
    commandErrors,
    setViewInput,
    runAction,
  };
}
