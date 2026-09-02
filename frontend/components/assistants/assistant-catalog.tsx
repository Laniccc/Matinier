"use client";

import type { AssistantCatalogEntry } from "@/types/plugins";


const STATUS_LABELS: Record<AssistantCatalogEntry["status"], string> = {
  ready: "已连接",
  waiting: "等待视图",
  disabled: "已停用",
  degraded: "运行降级",
  crashed: "运行崩溃",
  quarantined: "已隔离",
  historical: "历史记录",
};

export function AssistantCatalog({
  entries,
  selectedPluginId,
  onSelect,
  navigationTarget,
}: {
  entries: AssistantCatalogEntry[];
  selectedPluginId: string | null;
  onSelect: (pluginId: string) => void;
  navigationTarget?: "_blank";
}) {
  return (
    <nav className="assistantCatalog" aria-label="助手目录">
      <div className="assistantCatalogHeading">
        <span className="label">Assistants</span>
        <h2>助手目录</h2>
      </div>
      {entries.length === 0 ? (
        <div className="assistantCatalogEmpty">
          <p>当前 Session 尚无助手。</p>
          <a href="/plugins" target={navigationTarget} rel={navigationTarget ? "noopener noreferrer" : undefined}>前往安装插件</a>
        </div>
      ) : (
        <ul>
          {entries.map((entry) => (
            <li key={entry.pluginId}>
              <button
                type="button"
                className={entry.pluginId === selectedPluginId ? "active" : ""}
                aria-pressed={entry.pluginId === selectedPluginId}
                data-plugin-id={entry.pluginId}
                onClick={() => onSelect(entry.pluginId)}
              >
                <span>
                  <strong>{entry.name}</strong>
                  <small>{entry.pluginId}</small>
                </span>
                <i className={`assistantStatus assistantStatus-${entry.status}`}>
                  {STATUS_LABELS[entry.status]}
                </i>
                <em>{entry.views.length} 个视图 · {entry.documents.length} 份文档{entry.historySources?.length ? " · 主程序历史" : ""}</em>
              </button>
            </li>
          ))}
        </ul>
      )}
      {entries.length > 0 ? <a href="/plugins" target={navigationTarget} rel={navigationTarget ? "noopener noreferrer" : undefined}>安装或管理更多助手</a> : null}
    </nav>
  );
}
