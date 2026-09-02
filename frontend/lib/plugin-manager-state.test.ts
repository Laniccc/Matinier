import { describe, expect, it } from "vitest";

import {
  canStartPluginInspection,
  canConfirmPluginInstall,
  createPluginManagerState,
  reconcileBuiltinCatalog,
  reducePluginManagerState,
} from "./plugin-manager-state";
import type { BuiltinPluginSummary, InstalledPlugin } from "@/types/plugins";

const inspection = {
  ticket_id: "ticket-1",
  plugin_id: "com.example.viewer",
  version: "1.0.0",
  name: "Viewer",
  publisher_name: "Example",
  publisher_fingerprint: "sha256:publisher",
  publisher_trusted: false,
  signature_status: "verified",
  permissions: ["media.query", "ui.publish"],
  content_digest: "sha256:content",
  manifest_hash: "sha256:manifest",
  expires_at: "2026-08-28T12:00:00Z",
};

const builtin: BuiltinPluginSummary = {
  id: "com.matinier.course-organizer",
  name: "Course organizer",
  description: "Course notes",
  version: "1.0.0",
  dynamic_build_available: true,
};

const installed: InstalledPlugin = {
  plugin_id: builtin.id,
  name: builtin.name,
  status: "enabled",
  preferred_version: builtin.version,
  versions: [builtin.version],
  permissions: inspection.permissions,
  runtime_status: "ready",
  quarantine_reason: null,
};

describe("plugin manager state", () => {
  it("keeps package inspection and installation as two explicit phases", () => {
    let state = createPluginManagerState();
    state = reducePluginManagerState(state, {
      type: "select",
      file: new File(["zip"], "viewer.plugin.zip"),
    });
    state = reducePluginManagerState(state, { type: "inspect" });
    expect(state.phase).toBe("inspecting");

    state = reducePluginManagerState(state, {
      type: "inspected",
      inspection,
    });
    expect(state.phase).toBe("confirming");
    expect(state.acceptedPermissions).toEqual(new Set());
    expect(canConfirmPluginInstall(state)).toBe(false);

    for (const permission of inspection.permissions) {
      state = reducePluginManagerState(state, {
        type: "toggle_permission",
        permission,
      });
    }
    expect(canConfirmPluginInstall(state)).toBe(false);
    state = reducePluginManagerState(state, {
      type: "set_trust_publisher",
      value: true,
    });
    expect(canConfirmPluginInstall(state)).toBe(true);
    state = {
      ...state,
      acceptedPermissions: new Set([...inspection.permissions, "network.fetch"]),
    };
    expect(canConfirmPluginInstall(state)).toBe(false);
    state = {
      ...state,
      acceptedPermissions: new Set(inspection.permissions),
    };
    state = reducePluginManagerState(state, { type: "install" });
    expect(state.phase).toBe("installing");
  });

  it("clears stale inspection data on a new file and exposes safe errors", () => {
    let state = reducePluginManagerState(createPluginManagerState(), {
      type: "inspected",
      inspection,
    });
    state = reducePluginManagerState(state, {
      type: "set_trust_publisher",
      value: true,
    });
    state = reducePluginManagerState(state, {
      type: "select",
      file: new File(["new"], "new.plugin.zip"),
    });
    expect(state.inspection).toBeNull();
    expect(state.trustPublisher).toBe(false);
    expect(state.phase).toBe("idle");

    state = reducePluginManagerState(state, {
      type: "failed",
      message: "插件包校验失败",
    });
    expect(state.phase).toBe("error");
    expect(state.error).toBe("插件包校验失败");
  });

  it("loads the built-in catalog and reconciles installed state", () => {
    let state = reducePluginManagerState(createPluginManagerState(), {
      type: "catalog_loading",
    });
    expect(state.catalogPhase).toBe("loading");
    state = reducePluginManagerState(state, {
      type: "catalog_loaded",
      builtins: [builtin],
      installedPlugins: [installed],
    });

    expect(state.catalogPhase).toBe("ready");
    expect(reconcileBuiltinCatalog(state.builtins, state.installedPlugins)).toEqual([
      { builtin, installed },
    ]);
  });

  it("inspects a built-in source without requiring a File and blocks repeat clicks", () => {
    let state = reducePluginManagerState(createPluginManagerState(), {
      type: "catalog_loaded",
      builtins: [builtin],
      installedPlugins: [],
    });
    state = reducePluginManagerState(state, {
      type: "select_builtin",
      pluginId: builtin.id,
    });
    expect(state.source).toBe("builtin");
    expect(state.file).toBeNull();
    expect(canStartPluginInspection(state)).toBe(true);

    state = reducePluginManagerState(state, { type: "inspect" });
    const repeated = reducePluginManagerState(state, { type: "inspect" });
    expect(repeated).toBe(state);
    expect(canStartPluginInspection(state)).toBe(false);
  });

  it("models explicit confirm, install, then enable and retains disabled install on enable failure", () => {
    let state = reducePluginManagerState(createPluginManagerState(), {
      type: "select_builtin",
      pluginId: builtin.id,
    });
    state = reducePluginManagerState(state, { type: "inspected", inspection });
    for (const permission of inspection.permissions) {
      state = reducePluginManagerState(state, {
        type: "toggle_permission",
        permission,
      });
    }
    state = reducePluginManagerState(state, {
      type: "set_trust_publisher",
      value: true,
    });
    state = reducePluginManagerState(state, { type: "install" });
    state = reducePluginManagerState(state, {
      type: "installed",
      pluginId: builtin.id,
    });
    expect(state.phase).toBe("installed");
    state = reducePluginManagerState(state, { type: "enable" });
    expect(state.phase).toBe("enabling");
    state = reducePluginManagerState(state, {
      type: "enable_failed",
      message: "容器启动失败",
    });
    expect(state.phase).toBe("installed_disabled");
    expect(state.warning).toContain("容器启动失败");
    expect(state.installedPluginId).toBe(builtin.id);
  });

  it("preserves the uploaded package selection flow", () => {
    const file = new File(["zip"], "local.plugin.zip");
    let state = reducePluginManagerState(createPluginManagerState(), {
      type: "select",
      file,
    });
    expect(state.source).toBe("upload");
    expect(state.file).toBe(file);
    expect(state.selectedBuiltinId).toBeNull();
    expect(canStartPluginInspection(state)).toBe(true);
  });
});
