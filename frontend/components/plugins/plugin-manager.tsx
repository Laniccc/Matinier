"use client";

import { useCallback, useEffect, useReducer, useState } from "react";

import {
  confirmPluginInstall,
  createPluginGrant,
  disablePlugin,
  enablePlugin,
  inspectBuiltinPlugin,
  inspectPluginPackage,
  listBuiltinPlugins,
  listPlugins,
  uninstallPlugin,
} from "@/lib/api";
import {
  canStartPluginInspection,
  canConfirmPluginInstall,
  createPluginManagerState,
  isPluginManagerBusy,
  reconcileBuiltinCatalog,
  reducePluginManagerState,
} from "@/lib/plugin-manager-state";
import type { InstalledPlugin } from "@/types/plugins";

function capabilityEffect(capability: string) {
  if (capability === "network.fetch") return "network" as const;
  if (capability.startsWith("action.")) return "external_write" as const;
  if (
    capability === "state.put" ||
    capability === "ui.publish" ||
    capability === "delivery.prepare" ||
    capability === "document.publish"
  ) {
    return "local_write" as const;
  }
  return "read" as const;
}

function requiresEphemeralGrant(capability: string): boolean {
  const effect = capabilityEffect(capability);
  return (
    capability.startsWith("media.") ||
    effect === "network" ||
    effect === "external_write"
  );
}

export function PluginManager() {
  const [state, dispatch] = useReducer(reducePluginManagerState, undefined, createPluginManagerState);
  const [adminToken, setAdminToken] = useState("");
  const [plugins, setPlugins] = useState<InstalledPlugin[]>([]);
  const [listError, setListError] = useState<string | null>(null);
  const [busyPlugin, setBusyPlugin] = useState<string | null>(null);
  const [grantPluginId, setGrantPluginId] = useState("");
  const [grantCapability, setGrantCapability] = useState("");
  const [grantMediaSessionId, setGrantMediaSessionId] = useState("");
  const [grantScope, setGrantScope] = useState("{}");
  const [grantTtlSeconds, setGrantTtlSeconds] = useState(900);
  const [grantMessage, setGrantMessage] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    dispatch({ type: "catalog_loading" });
    try {
      const [builtins, installedPlugins] = await Promise.all([
        listBuiltinPlugins(),
        listPlugins(),
      ]);
      setPlugins(installedPlugins);
      dispatch({ type: "catalog_loaded", builtins, installedPlugins });
      setListError(null);
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : "插件列表加载失败";
      setListError(message);
      dispatch({ type: "catalog_failed", message });
    }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);

  const inspectUpload = async () => {
    if (state.file === null || adminToken.trim() === "") return;
    dispatch({ type: "inspect" });
    try {
      dispatch({ type: "inspected", inspection: await inspectPluginPackage(state.file, adminToken) });
    } catch (caught) {
      dispatch({ type: "failed", message: caught instanceof Error ? caught.message : "插件包校验失败" });
    }
  };

  const inspectBuiltin = async (pluginId: string) => {
    if (adminToken.trim() === "" || isPluginManagerBusy(state)) return;
    dispatch({ type: "select_builtin", pluginId });
    dispatch({ type: "inspect" });
    try {
      const nextInspection = await inspectBuiltinPlugin(pluginId, adminToken);
      dispatch({ type: "inspected", inspection: nextInspection });
    } catch (caught) {
      dispatch({
        type: "failed",
        message: caught instanceof Error ? caught.message : "内置插件检查失败",
      });
    }
  };

  const install = async () => {
    if (!canConfirmPluginInstall(state) || state.inspection === null) return;
    dispatch({ type: "install" });
    try {
      const installedPlugin = await confirmPluginInstall(state.inspection, [...state.acceptedPermissions], state.trustPublisher, adminToken);
      dispatch({ type: "installed", pluginId: installedPlugin.plugin_id });
      await refresh();
      if (state.source === "builtin") {
        dispatch({ type: "enable" });
        try {
          await enablePlugin(installedPlugin.plugin_id, adminToken);
          dispatch({ type: "enabled" });
        } catch (caught) {
          dispatch({
            type: "enable_failed",
            message: caught instanceof Error ? caught.message : "插件启用失败",
          });
        }
        await refresh();
      }
    } catch (caught) {
      dispatch({ type: "failed", message: caught instanceof Error ? caught.message : "插件安装失败" });
    }
  };

  const lifecycle = async (plugin: InstalledPlugin, operation: "enable" | "disable" | "delete") => {
    if (adminToken.trim() === "") {
      setListError("请先输入本机插件管理员令牌");
      return;
    }
    setBusyPlugin(plugin.plugin_id);
    try {
      if (operation === "enable") await enablePlugin(plugin.plugin_id, adminToken);
      if (operation === "disable") await disablePlugin(plugin.plugin_id, adminToken);
      if (operation === "delete") await uninstallPlugin(plugin.plugin_id, adminToken);
      await refresh();
    } catch (caught) {
      setListError(caught instanceof Error ? caught.message : "插件操作失败");
    } finally {
      setBusyPlugin(null);
    }
  };

  const grant = async () => {
    if (adminToken.trim() === "" || grantPluginId === "" || grantCapability === "") {
      setGrantMessage("请选择插件和权限，并输入管理员令牌");
      return;
    }
    try {
      const scope = JSON.parse(grantScope) as unknown;
      if (typeof scope !== "object" || scope === null || Array.isArray(scope)) {
        throw new Error("授权范围必须是 JSON 对象");
      }
      const result = await createPluginGrant(
        grantPluginId,
        {
          media_session_id: grantMediaSessionId.trim() || null,
          capability: grantCapability,
          effect: capabilityEffect(grantCapability),
          scope: scope as Record<string, unknown>,
          ttl_seconds: grantTtlSeconds,
        },
        adminToken,
      );
      setGrantMessage(`授权已创建：${result.grant_id}`);
    } catch (caught) {
      setGrantMessage(caught instanceof Error ? caught.message : "授权创建失败");
    }
  };

  const grantPlugin = plugins.find((plugin) => plugin.plugin_id === grantPluginId) ?? null;
  const grantCapabilities =
    grantPlugin?.permissions.filter(requiresEphemeralGrant) ?? [];
  const builtinCatalog = reconcileBuiltinCatalog(state.builtins, plugins);

  return (
    <main className="pluginManagerShell">
      <header className="pluginManagerHero">
        <div><p className="eyebrow">Local plugin control</p><h1>插件管理</h1><p>插件包先检查、再授权安装；运行代码只会进入隔离容器。</p></div>
        <nav className="pluginManagerNav" aria-label="插件页面导航">
          <a href="/assistants" className="pluginBackLink">助手工作区</a>
          <a href="/" className="pluginBackLink">返回直播控制台</a>
        </nav>
      </header>

      <section className="studioPanel pluginBuiltinPanel">
        <div className="panelHeading">
          <div><span className="label">Project catalog</span><h2>项目内置插件</h2></div>
          <span className="pluginPhase">{state.catalogPhase}</span>
        </div>
        <label className="pluginField">
          <span>本机管理员令牌（仅保存在当前页面内存）</span>
          <input
            type="password"
            value={adminToken}
            onChange={(event) => setAdminToken(event.target.value)}
            autoComplete="off"
          />
        </label>
        {state.catalogError ? <p className="pluginInlineError" role="alert">{state.catalogError}</p> : null}
        {state.error ? <p className="pluginInlineError" role="alert">{state.error}</p> : null}
        {state.warning ? <p className="pluginInstallWarning" role="alert">{state.warning}</p> : null}
        {state.catalogPhase === "loading" ? <p className="emptyState">正在加载内置插件目录…</p> : null}
        {state.catalogPhase === "ready" && builtinCatalog.length === 0 ? <p className="emptyState">当前构建没有内置插件。</p> : null}
        <div className="pluginBuiltinGrid">
          {builtinCatalog.map(({ builtin: item, installed: installedItem }) => (
            <article className="pluginBuiltinCard" key={item.id}>
              <header>
                <div><strong>{item.name}</strong><code>{item.id} · {item.version}</code></div>
                <span className={item.dynamic_build_available ? "launcherReady" : "launcherAlert"}>
                  {item.dynamic_build_available ? "可构建" : "构建已关闭"}
                </span>
              </header>
              <p>{item.description}</p>
              <dl>
                <div><dt>安装状态</dt><dd>{installedItem?.status ?? "未安装"}</dd></div>
                <div><dt>运行状态</dt><dd>{installedItem?.runtime_status ?? "--"}</dd></div>
              </dl>
              <button
                type="button"
                className="primaryAction"
                onClick={() => void inspectBuiltin(item.id)}
                disabled={
                  !item.dynamic_build_available ||
                  adminToken.trim() === "" ||
                  isPluginManagerBusy(state)
                }
              >
                {state.source === "builtin" && state.selectedBuiltinId === item.id && state.phase === "inspecting"
                  ? "正在检查…"
                  : installedItem
                    ? "重新检查内置插件"
                    : "检查内置插件"}
              </button>
            </article>
          ))}
        </div>
      </section>

      <section className="studioPanel pluginInstallPanel">
        <div className="panelHeading"><div><span className="label">Two-phase install</span><h2>安装本地插件包</h2></div><span className={`pluginPhase pluginPhase-${state.phase}`}>{state.phase}</span></div>
        <label className="pluginField"><span>.plugin.zip 插件包</span><input type="file" accept=".zip,.plugin.zip,application/zip" disabled={isPluginManagerBusy(state)} onChange={(event) => dispatch({ type: "select", file: event.target.files?.[0] ?? null })} /></label>
        <button type="button" className="primaryAction" onClick={() => void inspectUpload()} disabled={!canStartPluginInspection(state) || state.source !== "upload" || adminToken.trim() === ""}>{state.source === "upload" && state.phase === "inspecting" ? "正在检查…" : "检查插件包"}</button>
        {state.inspection ? (
          <div className="pluginInspection">
            <div><strong>{state.inspection.name}</strong><code>{state.inspection.plugin_id} · {state.inspection.version}</code></div>
            <dl><div><dt>发布者</dt><dd>{state.inspection.publisher_name}</dd></div><div><dt>签名</dt><dd>{state.inspection.signature_status}</dd></div><div><dt>指纹</dt><dd><code>{state.inspection.publisher_fingerprint}</code></dd></div></dl>
            <fieldset><legend>逐项接受权限</legend>{state.inspection.permissions.map((permission) => <label className="pluginCheckbox" key={permission}><input type="checkbox" checked={state.acceptedPermissions.has(permission)} onChange={() => dispatch({ type: "toggle_permission", permission })} /><span>{permission}</span></label>)}</fieldset>
            {!state.inspection.publisher_trusted && state.inspection.publisher_fingerprint !== null ? <label className="pluginCheckbox"><input type="checkbox" checked={state.trustPublisher} onChange={(event) => dispatch({ type: "set_trust_publisher", value: event.target.checked })} /><span>同时信任此发布者指纹</span></label> : null}
            <button type="button" className="primaryAction" onClick={() => void install()} disabled={!canConfirmPluginInstall(state) || state.phase === "installing" || state.phase === "enabling"}>{state.phase === "installing" ? "正在安装…" : state.phase === "enabling" ? "正在启用…" : state.source === "builtin" ? "确认、安装并启用" : "确认授权并安装"}</button>
          </div>
        ) : null}
      </section>

      <section className="studioPanel pluginListPanel">
        <div className="panelHeading"><div><span className="label">Installed</span><h2>已安装插件</h2></div><button type="button" className="iconButton" onClick={() => void refresh()} aria-label="刷新插件">↻</button></div>
        {listError ? <p className="pluginInlineError" role="alert">{listError}</p> : null}
        {plugins.length === 0 ? <p className="emptyState">尚未安装插件。</p> : <div className="installedPluginGrid">{plugins.map((plugin) => <article className="installedPlugin" key={plugin.plugin_id}><header><div><strong>{plugin.name}</strong><code>{plugin.plugin_id} · {plugin.preferred_version}</code></div><span className={`pluginRuntime pluginRuntime-${plugin.runtime_status}`}>{plugin.runtime_status}</span></header><div className="pluginPermissionList">{plugin.permissions.map((permission) => <span key={permission}>{permission}</span>)}</div>{plugin.quarantine_reason ? <p className="pluginInlineError">{plugin.quarantine_reason}</p> : null}<footer><button type="button" onClick={() => void lifecycle(plugin, plugin.status === "enabled" ? "disable" : "enable")} disabled={busyPlugin === plugin.plugin_id}>{plugin.status === "enabled" ? "停用" : "启用"}</button><button type="button" className="dangerButton" onClick={() => void lifecycle(plugin, "delete")} disabled={busyPlugin === plugin.plugin_id}>卸载</button></footer></article>)}</div>}
      </section>

      <section className="studioPanel pluginGrantPanel">
        <div className="panelHeading"><div><span className="label">Ephemeral authority</span><h2>创建限时授权</h2></div><span className="pluginPhase">30 秒–24 小时</span></div>
        <div className="pluginGrantGrid">
          <label className="pluginField"><span>插件</span><select value={grantPluginId} onChange={(event) => { setGrantPluginId(event.target.value); setGrantCapability(""); }}><option value="">请选择</option>{plugins.map((plugin) => <option key={plugin.plugin_id} value={plugin.plugin_id}>{plugin.name}</option>)}</select></label>
          <label className="pluginField"><span>需要限时授权的权限</span><select value={grantCapability} onChange={(event) => setGrantCapability(event.target.value)} disabled={grantPlugin === null || grantCapabilities.length === 0}><option value="">{grantPlugin !== null && grantCapabilities.length === 0 ? "此插件无需限时授权" : "请选择"}</option>{grantCapabilities.map((permission) => <option key={permission} value={permission}>{permission}</option>)}</select></label>
          <label className="pluginField"><span>MediaSession ID（会话能力必填）</span><input value={grantMediaSessionId} onChange={(event) => setGrantMediaSessionId(event.target.value)} placeholder="留空表示全局授权" /></label>
          <label className="pluginField"><span>有效期（秒）</span><input type="number" min={30} max={86400} value={grantTtlSeconds} onChange={(event) => setGrantTtlSeconds(Number(event.target.value))} /></label>
        </div>
        <label className="pluginField"><span>授权范围 JSON</span><textarea rows={4} value={grantScope} onChange={(event) => setGrantScope(event.target.value)} spellCheck={false} placeholder={'{"destinations":["api.example.com"],"methods":["GET"]}'} /></label>
        <button type="button" className="primaryAction" onClick={() => void grant()}>创建限时授权</button>
        {grantMessage ? <p className={grantMessage.startsWith("授权已创建") ? "pluginGrantSuccess" : "pluginInlineError"}>{grantMessage}</p> : null}
      </section>
    </main>
  );
}
