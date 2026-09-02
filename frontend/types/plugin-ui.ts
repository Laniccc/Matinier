export type PluginUISurface = "panel" | "overlay";
export type PluginUITone = "neutral" | "info" | "success" | "warning" | "danger";

export interface PluginUIAction {
  id: string;
  kind: "command";
  command: string;
}

interface PluginUIComponentBase {
  id: string;
}

export interface PluginUIText extends PluginUIComponentBase {
  type: "text";
  text: string;
}

export interface PluginUISafeMarkdown extends PluginUIComponentBase {
  type: "safe_markdown";
  markdown: string;
}

export interface PluginUICard extends PluginUIComponentBase {
  type: "card";
  title?: string;
  children: PluginUIComponent[];
}

export interface PluginUISection extends PluginUIComponentBase {
  type: "section";
  title: string;
  children: PluginUIComponent[];
}

export interface PluginUITab {
  id: string;
  label: string;
  children: PluginUIComponent[];
}

export interface PluginUITabs extends PluginUIComponentBase {
  type: "tabs";
  tabs: PluginUITab[];
}

export interface PluginUIListItem {
  id: string;
  primary: string;
  secondary?: string;
}

export interface PluginUIList extends PluginUIComponentBase {
  type: "list";
  items: PluginUIListItem[];
}

export interface PluginUITableColumn {
  id: string;
  label: string;
}

export interface PluginUITable extends PluginUIComponentBase {
  type: "table";
  columns: PluginUITableColumn[];
  rows: Record<string, string>[];
}

export interface PluginUITimelineItem {
  id: string;
  time_ms: number;
  title: string;
  body?: string;
}

export interface PluginUITimeline extends PluginUIComponentBase {
  type: "timeline";
  items: PluginUITimelineItem[];
}

export interface PluginUIMediaAnchor extends PluginUIComponentBase {
  type: "media_anchor";
  media_time_ms: number;
  label: string;
}

export interface PluginUIBadge extends PluginUIComponentBase {
  type: "badge";
  text: string;
  tone: PluginUITone;
}

export interface PluginUIMetric extends PluginUIComponentBase {
  type: "metric";
  label: string;
  value: string;
  detail?: string;
}

export interface PluginUIProgress extends PluginUIComponentBase {
  type: "progress";
  label: string;
  value: number;
}

export interface PluginUIInput extends PluginUIComponentBase {
  type: "input";
  name: string;
  label: string;
  placeholder?: string;
}

export interface PluginUITextarea extends PluginUIComponentBase {
  type: "textarea";
  name: string;
  label: string;
  placeholder?: string;
  rows: number;
}

export interface PluginUISelectOption {
  id: string;
  label: string;
  value: string;
}

export interface PluginUISelect extends PluginUIComponentBase {
  type: "select";
  name: string;
  label: string;
  options: PluginUISelectOption[];
}

export interface PluginUICheckbox extends PluginUIComponentBase {
  type: "checkbox";
  name: string;
  label: string;
  checked: boolean;
}

export interface PluginUIButton extends PluginUIComponentBase {
  type: "button";
  label: string;
  action_id: string;
  tone: "neutral" | "primary" | "danger";
}

export interface PluginUIConfirmation extends PluginUIComponentBase {
  type: "confirmation";
  title: string;
  body: string;
  confirm_action_id: string;
  cancel_action_id?: string;
}

export interface PluginUIEmptyState extends PluginUIComponentBase {
  type: "empty_state";
  title: string;
  message?: string;
}

export interface PluginUIErrorState extends PluginUIComponentBase {
  type: "error_state";
  title: string;
  message: string;
}

export type PluginUIComponent =
  | PluginUIText
  | PluginUISafeMarkdown
  | PluginUICard
  | PluginUISection
  | PluginUITabs
  | PluginUIList
  | PluginUITable
  | PluginUITimeline
  | PluginUIMediaAnchor
  | PluginUIBadge
  | PluginUIMetric
  | PluginUIProgress
  | PluginUIInput
  | PluginUITextarea
  | PluginUISelect
  | PluginUICheckbox
  | PluginUIButton
  | PluginUIConfirmation
  | PluginUIEmptyState
  | PluginUIErrorState;

export interface PluginUIViewDocument {
  schema_version: 1;
  surface: PluginUISurface;
  view_id: string;
  view_version: number;
  root: PluginUIComponent;
  actions: PluginUIAction[];
}

export type PluginUIViewParseResult =
  | { ok: true; view: PluginUIViewDocument }
  | {
      ok: false;
      view: PluginUIViewDocument;
      errorCode: "invalid_schema" | "stale_version";
    };
