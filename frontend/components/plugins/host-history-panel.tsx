"use client";
import { useEffect, useState } from "react";
import { getHostHistory } from "@/lib/api";
import { parsePluginUIView } from "@/lib/plugin-ui-schema";
import { PluginViewCard } from "./plugin-surface";
import type { HostHistoryPage, HostHistorySource } from "@/types/plugins";

export function HostHistoryPanel({ source }: { source: HostHistorySource }) {
  const [position, setPosition] = useState({ offset: 0, after: 0, executionId: "" });
  const [page, setPage] = useState<HostHistoryPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let disposed = false; setLoading(true);
    void getHostHistory(source, position.offset, position.after, position.executionId).then(value => {
      if (!disposed) { setPage(value); setError(null); }
    }).catch(caught => { if (!disposed) setError(caught instanceof Error ? caught.message : "历史读取失败"); })
      .finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
  }, [source.media_session_id, source.plugin_id, position, refresh]);
  const parsed = page ? parsePluginUIView(page.ui_view, { allowedCommands: new Set() }) : null;
  return <section className="hostHistoryPanel" aria-label="主程序只读历史">
    <h3>主程序保留的历史</h3>
    <p>只读原始记录；查看历史不会启动分析或恢复执行。</p>
    <button type="button" disabled={loading} onClick={() => setRefresh(value => value + 1)}>刷新历史</button>
    {page?.executions.length ? <label className="pluginField">执行详情<select aria-label="历史执行详情" value={position.executionId}
      onChange={event => setPosition({ offset: 0, after: 0, executionId: event.target.value })}>
      <option value="">全部记录</option>{page.executions.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}
    </select></label> : null}
    {error ? <p role="alert">{error}</p> : null}
    {loading ? <p role="status">正在读取历史…</p> : null}
    {parsed?.ok ? <PluginViewCard view={{ plugin_id: source.plugin_id, plugin_version: "host-history", session_scope: source.media_session_id,
      surface: parsed.view.surface, view_id: parsed.view.view_id, view_version: parsed.view.view_version, allowed_commands: [], safe: true, view: parsed.view }}
      values={{}} busy={true} onAction={() => {}} onValueChange={() => {}} /> : parsed ? <p role="alert">历史视图无法安全显示</p> : null}
    <nav aria-label="历史分页">
      <button type="button" disabled={loading || (!position.offset && !position.after)} onClick={() => setPosition(current => ({ ...current, offset: 0, after: 0 }))}>回到首页</button>
      <button type="button" disabled={loading || !page?.view.has_more} onClick={() => setPosition(current => ({ ...current, offset: page!.view.next_offset }))}>下一页记录</button>
      <button type="button" disabled={loading || !page?.view.has_more_events} onClick={() => setPosition(current => ({ ...current, after: page!.view.next_cursor }))}>下一页事件</button>
    </nav>
  </section>;
}
