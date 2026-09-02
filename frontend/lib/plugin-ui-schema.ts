import type {
  PluginUIAction,
  PluginUIComponent,
  PluginUIViewDocument,
  PluginUIViewParseResult,
} from "../types/plugin-ui";

const STABLE_ID = /^[A-Za-z][A-Za-z0-9_.-]{0,127}$/;
const COMMAND = /^[a-z][a-z0-9_-]{0,63}$/;
const MAX_DEPTH = 8;
const MAX_NODES = 200;
const MAX_TOTAL_STRING_CHARS = 64_000;
const MAX_TABLE_ROWS = 100;
const MAX_OPTIONS = 50;
const MAX_ACTIONS = 32;
const OVERLAY_COMPONENTS = new Set([
  "text",
  "safe_markdown",
  "media_anchor",
  "badge",
  "metric",
  "progress",
  "empty_state",
  "error_state",
]);

interface ParserOptions {
  allowedCommands: ReadonlySet<string>;
  previous?: PluginUIViewDocument;
  mediaDurationMs?: number;
}

interface ParseContext {
  ids: Set<string>;
  actionReferences: string[];
  nodeCount: number;
  stringChars: number;
  componentTypes: Set<string>;
  mediaAnchors: number[];
}

class SchemaError extends Error {}

export function parsePluginUIView(
  raw: unknown,
  options: ParserOptions,
): PluginUIViewParseResult {
  let parsed: PluginUIViewDocument;
  try {
    parsed = parseDocument(raw, options);
  } catch {
    return safeFailure("invalid_schema");
  }
  const previous = options.previous;
  if (
    previous &&
    previous.surface === parsed.surface &&
    previous.view_id === parsed.view_id &&
    parsed.view_version <= previous.view_version
  ) {
    return safeFailure("stale_version");
  }
  return { ok: true, view: parsed };
}

function parseDocument(raw: unknown, options: ParserOptions): PluginUIViewDocument {
  const value = record(raw);
  exactKeys(value, [
    "schema_version",
    "surface",
    "view_id",
    "view_version",
    "root",
    "actions",
  ]);
  if (value.schema_version !== 1) throw new SchemaError("schema version");
  const surface = enumeration(value.surface, ["panel", "overlay"] as const);
  const viewId = stableId(value.view_id);
  const viewVersion = integer(value.view_version, 1);
  const context: ParseContext = {
    ids: new Set(),
    actionReferences: [],
    nodeCount: 0,
    stringChars: 0,
    componentTypes: new Set(),
    mediaAnchors: [],
  };
  const root = parseComponent(value.root, context, 1);
  const actionsRaw = array(value.actions, MAX_ACTIONS);
  const actions = actionsRaw.map((item) => parseAction(item, context, options));
  const actionIds = new Set(actions.map((action) => action.id));
  if (actionIds.size !== actions.length) throw new SchemaError("duplicate actions");
  if (context.actionReferences.some((reference) => !actionIds.has(reference))) {
    throw new SchemaError("unknown action reference");
  }
  if (
    surface === "overlay" &&
    [...context.componentTypes].some((type) => !OVERLAY_COMPONENTS.has(type))
  ) {
    throw new SchemaError("overlay component");
  }
  if (
    options.mediaDurationMs !== undefined &&
    (options.mediaDurationMs < 0 ||
      context.mediaAnchors.some((anchor) => anchor > options.mediaDurationMs!))
  ) {
    throw new SchemaError("media anchor");
  }
  if (context.nodeCount > MAX_NODES || context.stringChars > MAX_TOTAL_STRING_CHARS) {
    throw new SchemaError("document bounds");
  }
  return {
    schema_version: 1,
    surface,
    view_id: viewId,
    view_version: viewVersion,
    root,
    actions,
  };
}

function parseComponent(
  raw: unknown,
  context: ParseContext,
  depth: number,
): PluginUIComponent {
  if (depth > MAX_DEPTH) throw new SchemaError("depth");
  const value = record(raw);
  const id = stableId(value.id);
  if (context.ids.has(id)) throw new SchemaError("duplicate id");
  context.ids.add(id);
  context.nodeCount += 1;
  if (context.nodeCount > MAX_NODES) throw new SchemaError("nodes");
  const type = string(value.type, 64, context);
  context.componentTypes.add(type);

  switch (type) {
    case "text":
      exactKeys(value, ["id", "type", "text"]);
      return { id, type, text: string(value.text, 32_000, context) };
    case "safe_markdown": {
      exactKeys(value, ["id", "type", "markdown"]);
      const markdown = string(value.markdown, 32_000, context);
      assertSafeMarkdown(markdown);
      return { id, type, markdown };
    }
    case "card":
      exactKeys(value, ["id", "type", "title", "children"]);
      return {
        id,
        type,
        ...(value.title === undefined
          ? {}
          : { title: string(value.title, 500, context) }),
        children: parseChildren(value.children, context, depth),
      };
    case "section":
      exactKeys(value, ["id", "type", "title", "children"]);
      return {
        id,
        type,
        title: string(value.title, 500, context),
        children: parseChildren(value.children, context, depth),
      };
    case "tabs": {
      exactKeys(value, ["id", "type", "tabs"]);
      const tabs = array(value.tabs, 20, 1).map((item) => {
        const tab = record(item);
        exactKeys(tab, ["id", "label", "children"]);
        return {
          id: stableId(tab.id),
          label: string(tab.label, 200, context),
          children: parseChildren(tab.children, context, depth),
        };
      });
      return { id, type, tabs };
    }
    case "list": {
      exactKeys(value, ["id", "type", "items"]);
      const items = array(value.items, 200).map((item) => {
        const row = record(item);
        exactKeys(row, ["id", "primary", "secondary"]);
        return {
          id: stableId(row.id),
          primary: string(row.primary, 2_000, context),
          ...(row.secondary === undefined
            ? {}
            : { secondary: string(row.secondary, 4_000, context) }),
        };
      });
      return { id, type, items };
    }
    case "table": {
      exactKeys(value, ["id", "type", "columns", "rows"]);
      const columns = array(value.columns, 20, 1).map((item) => {
        const column = record(item);
        exactKeys(column, ["id", "label"]);
        return {
          id: stableId(column.id),
          label: string(column.label, 200, context),
        };
      });
      const columnIds = new Set(columns.map((column) => column.id));
      const rows = array(value.rows, MAX_TABLE_ROWS).map((item) => {
        const row = record(item);
        const output: Record<string, string> = {};
        for (const [key, cell] of Object.entries(row)) {
          if (!columnIds.has(key)) throw new SchemaError("unknown table column");
          output[key] = string(cell, 4_000, context);
        }
        return output;
      });
      return { id, type, columns, rows };
    }
    case "timeline": {
      exactKeys(value, ["id", "type", "items"]);
      const items = array(value.items, 200).map((item) => {
        const row = record(item);
        exactKeys(row, ["id", "time_ms", "title", "body"]);
        return {
          id: stableId(row.id),
          time_ms: integer(row.time_ms, 0),
          title: string(row.title, 500, context),
          ...(row.body === undefined
            ? {}
            : { body: string(row.body, 4_000, context) }),
        };
      });
      return { id, type, items };
    }
    case "media_anchor": {
      exactKeys(value, ["id", "type", "media_time_ms", "label"]);
      const mediaTime = integer(value.media_time_ms, 0);
      context.mediaAnchors.push(mediaTime);
      return {
        id,
        type,
        media_time_ms: mediaTime,
        label: string(value.label, 500, context),
      };
    }
    case "badge":
      exactKeys(value, ["id", "type", "text", "tone"]);
      return {
        id,
        type,
        text: string(value.text, 200, context),
        tone:
          value.tone === undefined
            ? "neutral"
            : enumeration(value.tone, ["neutral", "info", "success", "warning", "danger"] as const),
      };
    case "metric":
      exactKeys(value, ["id", "type", "label", "value", "detail"]);
      return {
        id,
        type,
        label: string(value.label, 200, context),
        value: string(value.value, 500, context),
        ...(value.detail === undefined
          ? {}
          : { detail: string(value.detail, 1_000, context) }),
      };
    case "progress": {
      exactKeys(value, ["id", "type", "label", "value"]);
      const progress = number(value.value, 0, 100);
      return { id, type, label: string(value.label, 200, context), value: progress };
    }
    case "input":
      exactKeys(value, ["id", "type", "name", "label", "placeholder"]);
      return {
        id,
        type,
        name: stableId(value.name),
        label: string(value.label, 200, context),
        ...(value.placeholder === undefined
          ? {}
          : { placeholder: string(value.placeholder, 500, context) }),
      };
    case "textarea":
      exactKeys(value, ["id", "type", "name", "label", "placeholder", "rows"]);
      return {
        id,
        type,
        name: stableId(value.name),
        label: string(value.label, 200, context),
        ...(value.placeholder === undefined
          ? {}
          : { placeholder: string(value.placeholder, 500, context) }),
        rows: value.rows === undefined ? 4 : integer(value.rows, 2, 20),
      };
    case "select": {
      exactKeys(value, ["id", "type", "name", "label", "options"]);
      const options = array(value.options, MAX_OPTIONS, 1).map((item) => {
        const option = record(item);
        exactKeys(option, ["id", "label", "value"]);
        return {
          id: stableId(option.id),
          label: string(option.label, 200, context),
          value: string(option.value, 500, context),
        };
      });
      return {
        id,
        type,
        name: stableId(value.name),
        label: string(value.label, 200, context),
        options,
      };
    }
    case "checkbox":
      exactKeys(value, ["id", "type", "name", "label", "checked"]);
      return {
        id,
        type,
        name: stableId(value.name),
        label: string(value.label, 500, context),
        checked: value.checked === undefined ? false : boolean(value.checked),
      };
    case "button": {
      exactKeys(value, ["id", "type", "label", "action_id", "tone"]);
      const actionId = stableId(value.action_id);
      context.actionReferences.push(actionId);
      return {
        id,
        type,
        label: string(value.label, 200, context),
        action_id: actionId,
        tone:
          value.tone === undefined
            ? "neutral"
            : enumeration(value.tone, ["neutral", "primary", "danger"] as const),
      };
    }
    case "confirmation": {
      exactKeys(value, [
        "id",
        "type",
        "title",
        "body",
        "confirm_action_id",
        "cancel_action_id",
      ]);
      const confirmAction = stableId(value.confirm_action_id);
      const cancelAction =
        value.cancel_action_id === undefined ? undefined : stableId(value.cancel_action_id);
      context.actionReferences.push(confirmAction);
      if (cancelAction) context.actionReferences.push(cancelAction);
      return {
        id,
        type,
        title: string(value.title, 500, context),
        body: string(value.body, 4_000, context),
        confirm_action_id: confirmAction,
        ...(cancelAction ? { cancel_action_id: cancelAction } : {}),
      };
    }
    case "empty_state":
      exactKeys(value, ["id", "type", "title", "message"]);
      return {
        id,
        type,
        title: string(value.title, 500, context),
        ...(value.message === undefined
          ? {}
          : { message: string(value.message, 4_000, context) }),
      };
    case "error_state":
      exactKeys(value, ["id", "type", "title", "message"]);
      return {
        id,
        type,
        title: string(value.title, 500, context),
        message: string(value.message, 4_000, context),
      };
    default:
      throw new SchemaError("unknown component");
  }
}

function parseChildren(
  raw: unknown,
  context: ParseContext,
  parentDepth: number,
): PluginUIComponent[] {
  return array(raw, 500).map((item) => parseComponent(item, context, parentDepth + 1));
}

function parseAction(
  raw: unknown,
  context: ParseContext,
  options: ParserOptions,
): PluginUIAction {
  const value = record(raw);
  exactKeys(value, ["id", "kind", "command"]);
  const id = stableId(value.id);
  const kind = enumeration(value.kind, ["command"] as const);
  const command = string(value.command, 64, context);
  if (!COMMAND.test(command) || !options.allowedCommands.has(command)) {
    throw new SchemaError("undeclared command");
  }
  return { id, kind, command };
}

function assertSafeMarkdown(markdown: string): void {
  if (/<\s*\/?\s*[A-Za-z][^>]*>/.test(markdown)) throw new SchemaError("raw html");
  if (/\b(?:javascript|data|file):/i.test(markdown)) throw new SchemaError("unsafe link");
  for (const match of markdown.matchAll(/\[[^\]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)/g)) {
    try {
      const destination = new URL(match[1]!);
      if (destination.protocol !== "https:") throw new SchemaError("unsafe link");
    } catch {
      throw new SchemaError("unsafe link");
    }
  }
}

function record(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new SchemaError("object expected");
  }
  return value as Record<string, unknown>;
}

function exactKeys(value: Record<string, unknown>, allowed: readonly string[]): void {
  const permitted = new Set(allowed);
  if (Object.keys(value).some((key) => !permitted.has(key))) {
    throw new SchemaError("unknown field");
  }
}

function array(value: unknown, maximum: number, minimum = 0): unknown[] {
  if (!Array.isArray(value) || value.length < minimum || value.length > maximum) {
    throw new SchemaError("array bounds");
  }
  return value;
}

function string(value: unknown, maximum: number, context: ParseContext): string {
  if (typeof value !== "string" || value.length > maximum) throw new SchemaError("string");
  context.stringChars += value.length;
  if (context.stringChars > MAX_TOTAL_STRING_CHARS) throw new SchemaError("strings");
  return value;
}

function stableId(value: unknown): string {
  if (typeof value !== "string" || !STABLE_ID.test(value)) throw new SchemaError("stable id");
  return value;
}

function integer(value: unknown, minimum: number, maximum = Number.MAX_SAFE_INTEGER): number {
  if (!Number.isInteger(value) || (value as number) < minimum || (value as number) > maximum) {
    throw new SchemaError("integer");
  }
  return value as number;
}

function number(value: unknown, minimum: number, maximum: number): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < minimum || value > maximum) {
    throw new SchemaError("number");
  }
  return value;
}

function boolean(value: unknown): boolean {
  if (typeof value !== "boolean") throw new SchemaError("boolean");
  return value;
}

function enumeration<const T extends readonly string[]>(
  value: unknown,
  allowed: T,
): T[number] {
  if (typeof value !== "string" || !allowed.includes(value)) throw new SchemaError("enum");
  return value as T[number];
}

function safeFailure(
  errorCode: "invalid_schema" | "stale_version",
): PluginUIViewParseResult {
  return {
    ok: false,
    errorCode,
    view: {
      schema_version: 1,
      surface: "panel",
      view_id: "plugin-ui-error",
      view_version: 1,
      root: {
        id: "plugin-ui-error",
        type: "error_state",
        title: "Plugin view unavailable",
        message: "This plugin view could not be displayed safely.",
      },
      actions: [],
    },
  };
}
