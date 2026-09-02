import type { HostActionControl, HostActionDescriptor } from "@/types/host-actions";
import type { JsonValue } from "@/types/assistant";

export type HostActionPhase = "idle" | "preparing" | "awaiting_confirmation" | "submitting" | "accepted" | "error";
export interface HostActionState {
  contextKey: string; phase: HostActionPhase; requestId: string | null;
  operationId: string | null; error: string | null; retryable: boolean;
}
type Event = { type: "reset"; contextKey: string } | { type: "start"; requestId: string }
  | { type: "phase"; requestId: string; phase: HostActionPhase }
  | { type: "accepted"; requestId: string; operationId?: string }
  | { type: "error"; requestId: string; message: string; retryable: boolean };

export function createHostActionState(contextKey: string): HostActionState {
  return { contextKey, phase: "idle", requestId: null, operationId: null, error: null, retryable: false };
}
export function reduceHostAction(state: HostActionState, event: Event): HostActionState {
  if (event.type === "reset") return createHostActionState(event.contextKey);
  if (event.type === "start") return { ...createHostActionState(state.contextKey), phase: "preparing", requestId: event.requestId };
  if (event.requestId !== state.requestId) return state;
  if (event.type === "phase") return { ...state, phase: event.phase, error: null, retryable: false };
  if (event.type === "accepted") return { ...state, phase: "accepted", operationId: event.operationId ?? null, error: null, retryable: false };
  return { ...state, phase: "error", error: event.message, retryable: event.retryable };
}
export function hostContextKey(value: Pick<HostActionDescriptor, "media_session_id" | "plugin_id" | "plugin_version" | "authority_epoch" | "analysis_epoch" | "source_kind">): string {
  return [value.media_session_id, value.plugin_id, value.plugin_version, value.source_kind, value.authority_epoch, value.analysis_epoch].join(":");
}
export function buildHostArguments(action: HostActionControl, values: Record<string, unknown>): Record<string, JsonValue> {
  const result: Record<string, JsonValue> = { ...action.fixed_arguments };
  for (const field of action.fields) {
    const raw = values[field.name];
    if (field.type === "multi_select") {
      const selected = Array.isArray(raw) ? raw : [];
      if (selected.length > 50 || new Set(selected).size !== selected.length || selected.some(item => !field.options?.some(option => option.value === item))) throw new Error("选项已变化，请刷新");
      if (field.required && selected.length === 0) throw new Error(`请选择${field.label}`);
      result[field.name] = selected as string[];
    } else {
      const value = typeof raw === "string" ? raw.trim() : "";
      if (!value) { if (field.required) throw new Error(`请填写${field.label}`); continue; }
      if (value.length > (field.max_length ?? 4000)) throw new Error(`${field.label}过长`);
      if (field.type === "select") {
        const option = field.options?.find(item => item.value === value);
        if (!option) throw new Error("选项已变化，请刷新");
        Object.assign(result, option.arguments ?? {});
      }
      result[field.name] = value;
    }
  }
  return result;
}

// This copies data only. A plugin request never calls prepare/confirm automatically.
export function selectHostValues(action: HostActionControl, inputs: Record<string, unknown>): Record<string, unknown> {
  const values: Record<string, unknown> = {};
  for (const field of action.fields) {
    if (field.type === "multi_select") {
      values[field.name] = (field.options ?? []).filter(option => option.input_name && inputs[option.input_name] === true).map(option => option.value);
    } else {
      const raw = inputs[field.input_name ?? field.name];
      if (typeof raw === "string" && (field.type !== "select" || field.options?.some(option => option.value === raw))) values[field.name] = raw;
    }
  }
  return values;
}
