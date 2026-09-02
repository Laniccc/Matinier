import type { JsonValue } from "./assistant";

export interface HostActionOption { value: string; label: string; arguments?: Record<string, JsonValue>; input_name?: string }
export interface HostActionField {
  name: string; label: string; type: "text" | "textarea" | "select" | "multi_select";
  required?: boolean; max_length?: number; options?: HostActionOption[]; input_name?: string;
}
export interface HostActionControl {
  id: string; action: string; label: string; enabled: boolean; reason: string | null;
  trigger_command?: string; fields: HostActionField[]; fixed_arguments: Record<string, JsonValue>;
}
// Only obtained from the Host endpoint. A plugin view is never parsed as this type.
export interface HostActionDescriptor {
  source_kind: "host_actions" | "host_history";
  plugin_id: string; media_session_id: string; plugin_version: string | null; view_version: number | null;
  authority_epoch: number; analysis_epoch: number; actions: HostActionControl[];
}
export interface HostActionRequest {
  action: string; request_id: string; plugin_version?: string; view_version?: number;
  arguments: Record<string, JsonValue>;
}
export interface HostActionPreview {
  preview_id: string; preview_hash: string; action: string;
  effect: "local_write" | "external_write"; confirmation_required: boolean;
  team_id: string | null; max_side_effects: number;
  candidates: { candidate_id: string; content: { title: { value: string | null } } }[];
  expires_at: string;
}
export interface HostActionConfirmation { preview_id: string; preview_hash: string; confirmed: boolean }
export interface HostActionSelection { contextKey: string; actionId: string; values: Record<string, unknown>; serial: string }
export interface HostActionResult { status: "accepted" | "applied" | "cancelled" | "unknown"; operation_id?: string; action?: string }
