import type {
  BuiltinPluginSummary,
  InstalledPlugin,
  PluginInspection,
} from "@/types/plugins";

export type PluginPackageSource = "upload" | "builtin";
export type PluginManagerPhase =
  | "idle"
  | "inspecting"
  | "confirming"
  | "installing"
  | "installed"
  | "enabling"
  | "enabled"
  | "installed_disabled"
  | "error";

export interface PluginManagerState {
  phase: PluginManagerPhase;
  source: PluginPackageSource;
  file: File | null;
  selectedBuiltinId: string | null;
  inspection: PluginInspection | null;
  acceptedPermissions: Set<string>;
  trustPublisher: boolean;
  installedPluginId: string | null;
  error: string | null;
  warning: string | null;
  catalogPhase: "idle" | "loading" | "ready" | "error";
  builtins: BuiltinPluginSummary[];
  installedPlugins: InstalledPlugin[];
  catalogError: string | null;
}

export type PluginManagerAction =
  | { type: "select"; file: File | null }
  | { type: "select_builtin"; pluginId: string }
  | { type: "catalog_loading" }
  | {
      type: "catalog_loaded";
      builtins: BuiltinPluginSummary[];
      installedPlugins: InstalledPlugin[];
    }
  | { type: "catalog_failed"; message: string }
  | { type: "inspect" }
  | { type: "inspected"; inspection: PluginInspection }
  | { type: "toggle_permission"; permission: string }
  | { type: "set_trust_publisher"; value: boolean }
  | { type: "install" }
  | { type: "installed"; pluginId: string }
  | { type: "enable" }
  | { type: "enabled" }
  | { type: "enable_failed"; message: string }
  | { type: "failed"; message: string }
  | { type: "reset" };

export interface ReconciledBuiltinPlugin {
  builtin: BuiltinPluginSummary;
  installed: InstalledPlugin | null;
}

const BUSY_PHASES = new Set<PluginManagerPhase>([
  "inspecting",
  "installing",
  "enabling",
]);

export function createPluginManagerState(): PluginManagerState {
  return {
    phase: "idle",
    source: "upload",
    file: null,
    selectedBuiltinId: null,
    inspection: null,
    acceptedPermissions: new Set(),
    trustPublisher: false,
    installedPluginId: null,
    error: null,
    warning: null,
    catalogPhase: "idle",
    builtins: [],
    installedPlugins: [],
    catalogError: null,
  };
}

function resetInspection(
  state: PluginManagerState,
  source: PluginPackageSource,
): PluginManagerState {
  return {
    ...state,
    phase: "idle",
    source,
    inspection: null,
    acceptedPermissions: new Set(),
    trustPublisher: false,
    installedPluginId: null,
    error: null,
    warning: null,
  };
}

export function reducePluginManagerState(
  state: PluginManagerState,
  action: PluginManagerAction,
): PluginManagerState {
  if (
    BUSY_PHASES.has(state.phase) &&
    ["select", "select_builtin", "inspect", "install", "enable"].includes(
      action.type,
    )
  ) {
    return state;
  }

  switch (action.type) {
    case "select":
      return {
        ...resetInspection(state, "upload"),
        file: action.file,
        selectedBuiltinId: null,
      };
    case "select_builtin":
      return {
        ...resetInspection(state, "builtin"),
        file: null,
        selectedBuiltinId: action.pluginId,
      };
    case "catalog_loading":
      return { ...state, catalogPhase: "loading", catalogError: null };
    case "catalog_loaded":
      return {
        ...state,
        catalogPhase: "ready",
        builtins: [...action.builtins],
        installedPlugins: [...action.installedPlugins],
        catalogError: null,
      };
    case "catalog_failed":
      return {
        ...state,
        catalogPhase: "error",
        catalogError: action.message,
      };
    case "inspect":
      if (!canStartPluginInspection(state)) return state;
      return { ...state, phase: "inspecting", error: null, warning: null };
    case "inspected":
      return {
        ...state,
        phase: "confirming",
        inspection: action.inspection,
        acceptedPermissions: new Set(),
        trustPublisher: false,
        error: null,
        warning: null,
      };
    case "toggle_permission": {
      if (state.phase !== "confirming") return state;
      const acceptedPermissions = new Set(state.acceptedPermissions);
      if (acceptedPermissions.has(action.permission)) {
        acceptedPermissions.delete(action.permission);
      } else {
        acceptedPermissions.add(action.permission);
      }
      return { ...state, acceptedPermissions };
    }
    case "set_trust_publisher":
      if (state.phase !== "confirming" || state.inspection === null) return state;
      return { ...state, trustPublisher: action.value };
    case "install":
      if (!canConfirmPluginInstall(state)) return state;
      return { ...state, phase: "installing", error: null, warning: null };
    case "installed":
      return {
        ...state,
        phase: "installed",
        installedPluginId: action.pluginId,
        error: null,
      };
    case "enable":
      if (state.phase !== "installed" || state.installedPluginId === null) {
        return state;
      }
      return { ...state, phase: "enabling", error: null };
    case "enabled":
      return { ...state, phase: "enabled", error: null, warning: null };
    case "enable_failed":
      return {
        ...state,
        phase: "installed_disabled",
        error: null,
        warning: `插件已经安装，但启用失败：${action.message}`,
      };
    case "failed":
      return { ...state, phase: "error", error: action.message };
    case "reset": {
      const reset = createPluginManagerState();
      return {
        ...reset,
        catalogPhase: state.catalogPhase,
        builtins: state.builtins,
        installedPlugins: state.installedPlugins,
        catalogError: state.catalogError,
      };
    }
  }
}

export function isPluginManagerBusy(state: PluginManagerState): boolean {
  return BUSY_PHASES.has(state.phase);
}

export function canStartPluginInspection(state: PluginManagerState): boolean {
  if (isPluginManagerBusy(state)) return false;
  if (state.source === "upload") return state.file !== null;
  if (state.selectedBuiltinId === null) return false;
  return Boolean(
    state.builtins.find(
      (builtin) =>
        builtin.id === state.selectedBuiltinId &&
        builtin.dynamic_build_available,
    ),
  );
}

export function canConfirmPluginInstall(state: PluginManagerState): boolean {
  if (state.phase !== "confirming" || state.inspection === null) return false;
  const requested = new Set(state.inspection.permissions);
  const publisherAccepted =
    state.inspection.publisher_trusted ||
    state.inspection.publisher_fingerprint === null ||
    state.trustPublisher;
  return (
    publisherAccepted &&
    state.acceptedPermissions.size === requested.size &&
    [...requested].every((permission) =>
      state.acceptedPermissions.has(permission),
    )
  );
}

export function reconcileBuiltinCatalog(
  builtins: BuiltinPluginSummary[],
  installedPlugins: InstalledPlugin[],
): ReconciledBuiltinPlugin[] {
  const installedById = new Map(
    installedPlugins.map((plugin) => [plugin.plugin_id, plugin]),
  );
  return [...builtins]
    .sort(
      (left, right) =>
        left.name.localeCompare(right.name) || left.id.localeCompare(right.id),
    )
    .map((builtin) => ({
      builtin,
      installed: installedById.get(builtin.id) ?? null,
    }));
}
