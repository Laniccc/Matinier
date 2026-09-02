"use client";

import { useEffect, useReducer, useRef, useState } from "react";
import { createAssistantUIContext, prepareHostAction, confirmHostAction } from "@/lib/api";
import { buildHostArguments, createHostActionState, hostContextKey, reduceHostAction, selectHostValues } from "@/lib/host-action-state";
import type { HostActionDescriptor, HostActionPreview, HostActionRequest, HostActionSelection } from "@/types/host-actions";

interface Pending {
  request: HostActionRequest; nonce: string | null; preview: HostActionPreview | null;
  mediaId: string; history: boolean; confirmationApproved: boolean;
}

export function HostActionPanel({ descriptor, onAccepted, selection }: { descriptor: HostActionDescriptor; onAccepted?: () => void; selection?: HostActionSelection | null }) {
  const key = hostContextKey(descriptor);
  const [state, dispatch] = useReducer(reduceHostAction, key, createHostActionState);
  const [selected, setSelected] = useState(descriptor.actions[0]?.id ?? "");
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [preview, setPreview] = useState<HostActionPreview | null>(null);
  const generation = useRef(0);
  const activeKey = useRef(key);
  activeKey.current = key;
  const pending = useRef<Pending | null>(null);
  const busy = useRef(false);
  const panel = useRef<HTMLElement | null>(null);
  const action = descriptor.actions.find(item => item.id === selected) ?? descriptor.actions[0];
  const phaseBusy = ["preparing", "submitting"].includes(state.phase);

  function clear() {
    generation.current += 1; pending.current = null; busy.current = false; setPreview(null);
    dispatch({ type: "reset", contextKey: key });
  }
  useEffect(() => {
    clear(); setValues({}); setSelected(descriptor.actions[0]?.id ?? "");
    return () => { generation.current += 1; pending.current = null; busy.current = false; };
    // Scope identity, not polling view versions, owns pending UI authority.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  useEffect(() => {
    if (!selection || selection.contextKey !== key) return;
    const requested = descriptor.actions.find(item => item.id === selection.actionId);
    if (!requested) return;
    clear(); setSelected(requested.id); setValues(selectHostValues(requested, selection.values));
    panel.current?.focus(); panel.current?.scrollIntoView?.({ block: "nearest" });
    // A user must explicitly submit from this Host panel after a plugin request.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selection, key]);

  useEffect(() => {
    if (!preview) return;
    const requestId = pending.current?.request.request_id;
    const timer = window.setTimeout(() => {
      generation.current += 1; pending.current = null; busy.current = false; setPreview(null);
      if (requestId) dispatch({ type: "error", requestId, message: "预览已过期，请重新提交", retryable: false });
    }, Math.max(0, Date.parse(preview.expires_at) - Date.now()));
    return () => window.clearTimeout(timer);
  }, [preview]);

  async function perform(item: Pending) {
    if (busy.current) return;
    busy.current = true;
    const expected = generation.current;
    const current = () => expected === generation.current && activeKey.current === key && pending.current === item;
    try {
      if (!item.nonce) {
        const context = await createAssistantUIContext();
        if (!current()) return;
        item.nonce = context.ui_nonce;
      }
      if (!item.preview) {
        dispatch({ type: "phase", requestId: item.request.request_id, phase: "preparing" });
        const next = await prepareHostAction(item.mediaId, item.request, item.nonce, item.history);
        if (!current()) return;
        if (next.action !== item.request.action || !Number.isFinite(Date.parse(next.expires_at))) throw new Error("主程序预览不匹配");
        item.preview = next;
      }
      if (Date.parse(item.preview.expires_at) <= Date.now()) throw new Error("预览已过期，请重新提交");
      if (item.preview.confirmation_required && !item.confirmationApproved) {
        setPreview(item.preview);
        dispatch({ type: "phase", requestId: item.request.request_id, phase: "awaiting_confirmation" });
        return;
      }
      dispatch({ type: "phase", requestId: item.request.request_id, phase: "submitting" });
      const result = await confirmHostAction(item.mediaId, { preview_id: item.preview.preview_id, preview_hash: item.preview.preview_hash, confirmed: true }, item.nonce);
      if (!current()) return;
      if (result.status === "unknown") throw new TypeError("受理结果未知，请重试原请求");
      if (result.status === "cancelled") throw new Error("操作已取消");
      if ((result.status !== "accepted" && result.status !== "applied") || (result.status === "accepted" && !result.operation_id)) throw new TypeError("受理响应无效，请重试原请求");
      pending.current = null; setPreview(null);
      dispatch({ type: "accepted", requestId: item.request.request_id, operationId: result.operation_id });
      onAccepted?.();
    } catch (caught) {
      if (!current()) return;
      const status = (caught as { status?: number })?.status;
      const retryable = caught instanceof TypeError || (typeof status === "number" && status >= 500);
      if (!retryable) { pending.current = null; setPreview(null); }
      dispatch({ type: "error", requestId: item.request.request_id, message: caught instanceof Error ? caught.message : "操作失败", retryable });
    } finally { if (expected === generation.current) busy.current = false; }
  }

  function start() {
    if (!action?.enabled || busy.current) return;
    const requestId = crypto.randomUUID();
    dispatch({ type: "start", requestId });
    try {
      const request: HostActionRequest = { action: action.action, request_id: requestId,
        ...(descriptor.plugin_version ? { plugin_version: descriptor.plugin_version } : {}),
        ...(descriptor.view_version ? { view_version: descriptor.view_version } : {}),
        arguments: buildHostArguments(action, values) };
      const item = { request, nonce: null, preview: null, confirmationApproved: false, mediaId: descriptor.media_session_id, history: descriptor.source_kind === "host_history" };
      pending.current = item; void perform(item);
    } catch (caught) {
      dispatch({ type: "error", requestId, message: caught instanceof Error ? caught.message : "输入无效", retryable: false });
    }
  }
  function cancel() {
    const item = pending.current;
    clear();
    if (item?.nonce && item.preview) void confirmHostAction(item.mediaId, { preview_id: item.preview.preview_id, preview_hash: item.preview.preview_hash, confirmed: false }, item.nonce).catch(() => {});
  }
  const edit = (name: string, value: unknown) => { clear(); setValues(current => ({ ...current, [name]: value })); };

  return <section ref={panel} tabIndex={-1} className="hostActionPanel" aria-label="主程序可信操作区">
    <h3>主程序操作区</h3>
    <p>授权由主程序验证，插件内容和确认按钮不能授予权限。</p>
    {descriptor.actions.length ? <>
      <label className="pluginField"><span>操作</span><select aria-label="主程序操作" value={action?.id ?? ""} disabled={phaseBusy}
        onChange={event => { clear(); setValues({}); setSelected(event.target.value); }}>
        {descriptor.actions.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
      </select></label>
      {action?.reason ? <p className="assistantNotice">{action.reason}</p> : null}
      {action?.fields.map(field => <label className="pluginField" key={field.name}><span>{field.label}</span>
        {field.type === "select" || field.type === "multi_select" ? <select aria-label={field.label} multiple={field.type === "multi_select"} disabled={phaseBusy || !action.enabled}
          value={(values[field.name] as string | string[] | undefined) ?? (field.type === "multi_select" ? [] : "")}
          onChange={event => edit(field.name, field.type === "multi_select" ? [...event.target.selectedOptions].map(option => option.value) : event.target.value)}>
          {field.type === "select" ? <option value="">请选择</option> : null}
          {field.options?.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select> : field.type === "textarea" ? <textarea aria-label={field.label} rows={3} maxLength={field.max_length ?? 4000} disabled={phaseBusy || !action.enabled}
          value={String(values[field.name] ?? "")} onChange={event => edit(field.name, event.target.value)} /> : <input aria-label={field.label} maxLength={field.max_length ?? 4000} disabled={phaseBusy || !action.enabled}
          value={String(values[field.name] ?? "")} onChange={event => edit(field.name, event.target.value)} />}
      </label>)}
      {!preview && !phaseBusy && !state.retryable ? <button type="button" disabled={!action?.enabled} onClick={start}>提交操作</button> : null}
      {phaseBusy ? <p role="status">{state.phase === "preparing" ? "正在准备…" : "正在受理…"}</p> : null}
      {preview ? <div className="hostActionPreview">
        <strong>主程序执行预览</strong><p>固定团队：{preview.team_id ?? "无外部写入"}</p><p>最多创建 {preview.max_side_effects} 项</p>
        <ul>{preview.candidates.map(item => <li key={item.candidate_id}>{item.content.title.value ?? item.candidate_id}</li>)}</ul>
        <p>有效期至 {new Date(preview.expires_at).toLocaleTimeString()}</p>
        <button type="button" disabled={phaseBusy} onClick={() => { if (pending.current) { pending.current.confirmationApproved = true; void perform(pending.current); } }}>确认执行</button>
      </div> : null}
      {state.retryable ? <button type="button" onClick={() => { if (pending.current) void perform(pending.current); }}>重试原请求</button> : null}
      {state.requestId && state.phase !== "accepted" ? <button type="button" onClick={cancel}>取消</button> : null}
      {state.error ? <p className="pluginInlineError" role="alert">{state.error}</p> : null}
      {state.phase === "accepted" ? <p role="status">已受理{state.operationId ? ` · ${state.operationId}` : ""}，结果将在助手视图或历史中更新。</p> : null}
    </> : <p className="assistantNotice">此来源当前没有可用操作。</p>}
  </section>;
}
