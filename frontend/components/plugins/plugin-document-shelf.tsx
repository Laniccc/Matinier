"use client";

import { useEffect, useMemo, useReducer } from "react";

import {
  getPluginDocumentExportUrl,
  listPluginDocuments,
} from "@/lib/api";
import {
  createPluginDocumentState,
  groupPluginDocuments,
  reducePluginDocumentState,
} from "@/lib/plugin-document-state";
import type {
  PluginDocumentCompleteness,
  PluginDocumentSummary,
  PluginDocumentTrigger,
} from "@/types/plugins";
import { filterAssistantDocuments } from "@/lib/assistant-workspace-state";

const COMPLETENESS_LABELS: Record<PluginDocumentCompleteness, string> = {
  interim: "实时笔记",
  complete: "完整整理",
  partial_terminal: "部分整理",
};

const TRIGGER_LABELS: Record<PluginDocumentTrigger, string> = {
  manual: "手动生成",
  session_completed: "课程结束",
  session_failed: "任务异常结束",
  session_cancelled: "任务取消",
};

function formatCreatedAt(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString();
}

export function PluginDocumentShelf({
  mediaSessionId,
  pluginId,
  documents,
  externalError = null,
}: {
  mediaSessionId: string;
  pluginId?: string;
  documents?: PluginDocumentSummary[];
  externalError?: string | null;
}) {
  const [state, dispatch] = useReducer(
    reducePluginDocumentState,
    undefined,
    createPluginDocumentState,
  );

  useEffect(() => {
    if (documents !== undefined) return;
    let disposed = false;
    let timer: number | null = null;

    const load = async (initial: boolean) => {
      dispatch({ type: initial ? "loading" : "refreshing" });
      try {
        const documents = await listPluginDocuments(mediaSessionId);
        if (!disposed) dispatch({ type: "loaded", documents });
      } catch (caught) {
        if (!disposed) {
          dispatch({
            type: "failed",
            message:
              caught instanceof Error ? caught.message : "文档历史加载失败",
          });
        }
      }
    };

    const start = async () => {
      await load(true);
      if (!disposed) {
        timer = window.setInterval(() => void load(false), 4_000);
      }
    };
    void start();

    return () => {
      disposed = true;
      if (timer !== null) window.clearInterval(timer);
    };
  }, [documents, mediaSessionId]);

  const externalGroups = useMemo(
    () =>
      documents === undefined
        ? null
        : groupPluginDocuments(
            pluginId ? filterAssistantDocuments(documents, pluginId) : documents,
          ),
    [documents, pluginId],
  );
  const groups = externalGroups ?? (
    pluginId
      ? state.groups.filter((group) => group.pluginId === pluginId)
      : state.groups
  );
  const error = externalError ?? state.error;
  const loading = documents === undefined && state.status === "loading";

  return (
    <section className="pluginDocumentShelf" aria-labelledby="plugin-documents-heading">
      <header>
        <div>
          <span className="label">Trusted documents</span>
          <h3 id="plugin-documents-heading">助手文档</h3>
        </div>
        {state.status === "refreshing" ? <span>正在刷新…</span> : null}
      </header>

      {loading ? (
        <p className="pluginDocumentStatus">正在加载文档历史…</p>
      ) : null}
      {error ? (
        <p className="pluginInlineError" role="alert">{error}</p>
      ) : null}
      {!loading && groups.length === 0 && error === null ? (
        <p className="pluginDocumentStatus">助手尚未发布文档。</p>
      ) : null}

      <div className="pluginDocumentGroups">
        {groups.map((group) => (
          <article className="pluginDocumentGroup" key={group.key}>
            <header>
              <div>
                <strong>{group.identityKey}</strong>
                <small>{group.pluginId}</small>
              </div>
              <span className="pluginDocumentLanguage">{group.language}</span>
            </header>
            <ol className="pluginDocumentVersions">
              {group.versions.map((document) => (
                <li key={document.document_id}>
                  <div className="pluginDocumentMeta">
                    <strong>v{document.document_version}</strong>
                    <span>{COMPLETENESS_LABELS[document.completeness]}</span>
                    <span>{TRIGGER_LABELS[document.trigger]}</span>
                    <time dateTime={document.created_at}>
                      {formatCreatedAt(document.created_at)}
                    </time>
                  </div>
                  <div className="pluginDocumentDownloads">
                    <a
                      target="_blank" rel="noopener noreferrer"
                      href={getPluginDocumentExportUrl(
                        document.document_id,
                        "markdown",
                      )}
                    >
                      Markdown
                    </a>
                    <a
                      target="_blank" rel="noopener noreferrer"
                      href={getPluginDocumentExportUrl(
                        document.document_id,
                        "json",
                      )}
                    >
                      JSON
                    </a>
                  </div>
                </li>
              ))}
            </ol>
          </article>
        ))}
      </div>
    </section>
  );
}
