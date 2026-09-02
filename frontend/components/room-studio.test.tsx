// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RoomStudio } from "./room-studio";
import { RoomAssistantSidebar } from "./assistants/room-assistant-sidebar";

const fake = vi.hoisted(() => {
  const audio = { kind: "audio", stop: vi.fn(), mediaStreamTrack: { addEventListener: vi.fn() } };
  const video = { kind: "video", stop: vi.fn(), mediaStreamTrack: { addEventListener: vi.fn() } };
  return {
    audio, video,
    handlers: new Map<string, (...args: unknown[]) => void>(),
    connect: vi.fn(async () => undefined), disconnect: vi.fn(async () => undefined),
    publish: vi.fn(async () => undefined),
    unpublish: vi.fn(async (track: typeof audio, stop: boolean) => { if (stop) track.stop(); }),
    capture: vi.fn(async () => [audio, video]),
    bridge: vi.fn(async (id: string) => ({ media_session_id: `media-${id}` })),
    health: vi.fn(async () => ({ framework_enabled: true })),
    plugins: vi.fn(), views: vi.fn(), documents: vi.fn(), command: vi.fn(), hostActions: vi.fn(), history: vi.fn(), prepare: vi.fn(), confirm: vi.fn(),
  };
});

vi.mock("livekit-client", () => ({
  ConnectionState: { Connected: "connected" },
  Track: { Kind: { Audio: "audio", Video: "video" }, Source: { ScreenShareAudio: "screen_share_audio", Microphone: "microphone" } },
  RoomEvent: new Proxy({}, { get: (_target, key) => key }),
  LocalAudioTrack: class {},
  Room: class {
    state = "connected";
    localParticipant = { identity: "test-user", trackPublications: new Map(), publishTrack: fake.publish, unpublishTrack: fake.unpublish };
    remoteParticipants = new Map();
    on(event: string, callback: (...args: unknown[]) => void) { fake.handlers.set(event, callback); return this; }
    removeAllListeners() { fake.handlers.clear(); }
    connect = fake.connect;
    disconnect = fake.disconnect;
  },
  createLocalScreenTracks: fake.capture,
  createLocalAudioTrack: vi.fn(async () => fake.audio),
}));

vi.mock("@/hooks/use-element-width", () => ({ useElementWidth: () => 520 }));
vi.mock("@/lib/caption-measurement", () => ({ createCanvasTextMeasurer: () => (text: string) => text.length * 8 }));
vi.mock("@/components/floating-captions", () => ({ FloatingCaptions: () => null }));
vi.mock("@/lib/api", () => ({
  listRooms: vi.fn(async () => [{ id: "room-1", room_name: "test-room", display_name: "测试直播间", status: "ready", updated_at: "2026-08-31T00:00:00Z" }]),
  listCaptionRuns: vi.fn(async () => []),
  createRoomToken: vi.fn(async () => ({ url: "ws://fake.invalid", token: "test" })),
  createCaptionRun: vi.fn(async () => ({ id: "session-1", room_id: "room-1", status: "running", source_type: "screen", source_name: "tab", language: "zh-CN", target_language: "en-US", translation_status: "running", created_at: "2026-08-31T00:00:00Z" })),
  getSessionRuntime: vi.fn(async () => ({ session_status: "running", audio_bytes: 0, audio_queue_current: null, started_at: null, last_event_at: null, unavailable: [] })),
  getSegments: vi.fn(async () => []), getTranslations: vi.fn(async () => []),
  getSessionExportUrl: (id: string, format: string) => `/api/sessions/${id}/export/${format}`,
  getPluginDocumentExportUrl: (id: string, format: string) => `/api/documents/${id}/${format}`,
  resolveMediaSession: fake.bridge, getPluginFrameworkHealth: fake.health,
  listPlugins: fake.plugins, listPluginViews: fake.views, listPluginDocuments: fake.documents,
  executePluginCommand: fake.command,
  getAssistantHistorySources: fake.history, getHostActionDescriptors: fake.hostActions,
  getHostHistory: vi.fn(async () => ({ ui_view: { schema_version: 1, surface: "panel", view_id: "history", view_version: 1, actions: [],
    root: { id: "old", type: "text", text: "保留的历史回答" } }, view: { offset: 0, next_offset: 10, has_more: false, next_cursor: 0, has_more_events: false }, executions: [] })),
  createAssistantUIContext: vi.fn(async () => ({ ui_nonce: "test-memory" })), prepareHostAction: fake.prepare, confirmHostAction: fake.confirm,
}));

function installed(pluginId: string) {
  return { plugin_id: pluginId, name: pluginId === "plugin.course" ? "课程整理" : "通用助手", status: "enabled", runtime_status: "ready", permissions: [], versions: ["1.0.0"], preferred_version: "1.0.0", quarantine_reason: null };
}

function view(pluginId: string) {
  return {
    plugin_id: pluginId, plugin_version: "1.0.0", session_scope: "scope", surface: "panel",
    view_id: "main", view_version: 1, allowed_commands: ["save"],
    view: {
      schema_version: 1, surface: "panel", view_id: "main", view_version: 1,
      root: { id: "root", type: "section", title: "实时笔记", children: [
        { id: "note", type: "input", name: "note", label: "笔记", placeholder: "输入笔记" },
        { id: "save", type: "button", label: "保存笔记", action_id: "save", tone: "primary" },
      ] },
      actions: [{ id: "save", kind: "command", command: "save" }],
    },
  };
}

function assistantDocument(pluginId: string, id: string) {
  return {
    document_id: id, plugin_id: pluginId, plugin_version: "1.0.0", media_session_id: "media-session-1",
    source_package_id: "package-1", source_package_version: 1, source_package_hash: "a".repeat(64),
    identity_key: "notes:zh-CN", document_version: 1, schema_name: "notes", schema_version: "1.0",
    language: "zh-CN", trigger: "manual", completeness: "interim", status: "published",
    content_hash: "b".repeat(64), created_at: "2026-08-31T00:00:00Z",
  };
}

describe("RoomStudio persistent assistant sidebar", () => {
  let container: HTMLDivElement;
  let root: Root | null;
  beforeEach(() => {
    vi.clearAllMocks();
    fake.handlers.clear();
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    // Any accidentally unmocked network operation must fail, never reach a real service.
    vi.stubGlobal("fetch", vi.fn(() => { throw new Error("Network forbidden in sidebar tests"); }));
    fake.health.mockResolvedValue({ framework_enabled: true });
    fake.plugins.mockResolvedValue([installed("plugin.course"), installed("plugin.generic")]);
    fake.views.mockResolvedValue([view("plugin.course"), view("plugin.generic")]);
    fake.documents.mockResolvedValue([]);
    fake.command.mockResolvedValue({});
    fake.hostActions.mockResolvedValue([]);
    fake.history.mockResolvedValue([]);
    fake.confirm.mockResolvedValue({ status: "accepted", operation_id: "op-test" });
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(async () => {
    if (root) await act(async () => root?.unmount());
    container.remove();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });
  const mount = async () => { await act(async () => root?.render(<RoomStudio />)); };
  function button(text: string) {
    const match = [...container.querySelectorAll("button")].find((node) =>
      node.getAttribute("aria-label") === text || node.textContent?.trim() === text);
    expect(match, `button: ${text}`).toBeDefined();
    return match!;
  }
  async function click(node: Element) { await act(async () => (node as HTMLElement).click()); }
  async function startTabAudio() {
    await mount();
    await click(container.querySelector(".roomListItem")!);
    await click([...container.querySelectorAll("button")].find((node) => node.textContent?.includes("标签页 / 系统音频"))!);
    await click(button("开始生成字幕"));
    expect(fake.capture).toHaveBeenCalledOnce();
    expect(fake.publish).toHaveBeenCalledOnce();
  }
  async function typeNote(text: string) {
    const input = container.querySelector<HTMLInputElement>('.assistantDetail input[placeholder="输入笔记"]')!;
    expect(input).not.toBeNull();
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, text);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    return input;
  }
  const selectPlugin = (id: string) => container.querySelector(`.assistantCatalog button[data-plugin-id="${id}"]`)!;
  function assertMediaStillLive(center: Element | null) {
    expect(container.querySelector(".studioCenter")).toBe(center);
    expect(fake.capture).toHaveBeenCalledOnce();
    expect(fake.publish).toHaveBeenCalledOnce();
    expect(fake.unpublish).not.toHaveBeenCalled();
    expect(fake.disconnect).not.toHaveBeenCalled();
    expect(fake.audio.stop).not.toHaveBeenCalled();
    expect(fake.video.stop).not.toHaveBeenCalled();
  }

  it("has one meeting entry and keeps audio through Host ask, confirm, cancel and plugin switches", async () => {
    const meeting = "com.matinier.meeting-assistant";
    fake.plugins.mockResolvedValue([installed("plugin.course"), { ...installed(meeting), name: "会议助手" }]);
    fake.views.mockResolvedValue([view("plugin.course"), view(meeting)]);
    fake.hostActions.mockResolvedValue([{ source_kind: "host_actions", plugin_id: meeting, media_session_id: "media-session-1", plugin_version: "1.0.0", view_version: 1,
      authority_epoch: 1, analysis_epoch: 1, actions: ["ask", "execute"].map(action => ({ id: action, action: `meeting.${action}`, label: action, enabled: true, reason: null, fields: [], fixed_arguments: { message: "Test" } })) }]);
    fake.prepare.mockImplementation(async (_media, request) => ({ preview_id: "preview", preview_hash: "hash", action: request.action, effect: request.action === "meeting.execute" ? "external_write" : "local_write",
      confirmation_required: request.action === "meeting.execute", team_id: "TEAM", max_side_effects: 0, candidates: [], expires_at: new Date(Date.now() + 60000).toISOString() }));
    await startTabAudio(); const center = container.querySelector(".studioCenter");
    expect(container.textContent).not.toContain("独立会议入口");
    expect(container.querySelector(".assistantPanel")).toBeNull();
    await click(button("助手工作区")); await click(selectPlugin("plugin.course")); await typeNote("课程输入保持");
    await click(selectPlugin(meeting)); await click(button("提交操作"));
    expect(fake.confirm).toHaveBeenCalledTimes(1);
    const select = container.querySelector<HTMLSelectElement>('select[aria-label="主程序操作"]')!;
    await act(async () => { select.value = "execute"; select.dispatchEvent(new Event("change", { bubbles: true })); });
    await click(button("提交操作")); expect(fake.confirm).toHaveBeenCalledTimes(1);
    await click(button("取消")); expect(fake.confirm.mock.calls.at(-1)?.[1].confirmed).toBe(false);
    await click(button("提交操作")); await click(button("确认执行"));
    await click(selectPlugin("plugin.course"));
    expect(container.querySelector<HTMLInputElement>('.assistantDetail input')!.value).toBe("课程输入保持");
    assertMediaStillLive(center);
  });

  it("shows Host-only history with the entire plugin framework disabled", async () => {
    fake.health.mockResolvedValue({ framework_enabled: false });
    fake.history.mockResolvedValue([{ source_kind: "host_history", plugin_id: "meeting.history", name: "会议历史", media_session_id: "old-media", legacy_session_id: "old" }]);
    await act(async () => root?.render(<RoomAssistantSidebar legacySessionId="old" />));
    expect(container.textContent).toContain("会议历史"); expect(container.textContent).toContain("保留的历史回答");
    expect(fake.bridge).not.toHaveBeenCalled(); expect(fake.command).not.toHaveBeenCalled(); expect(fake.prepare).not.toHaveBeenCalled();
    expect(container.querySelector('a[href="/plugins"]')?.getAttribute("target")).toBe("_blank");
  });

  it("renders the unresolved Linear assignee notice through the generic plugin text component", async () => {
    const meeting = "com.matinier.meeting-assistant";
    const notice = "负责人未能映射到 Linear 用户，已把会议中的原称呼保留在 Issue 描述；请在 Linear 中确认负责人。";
    const warningView = view(meeting);
    warningView.view.root.children.unshift({ id: "assignee-warning", type: "text", text: notice } as never);
    fake.plugins.mockResolvedValue([{ ...installed(meeting), name: "会议助手" }]);
    fake.views.mockResolvedValue([warningView]);
    await act(async () => root?.render(<RoomAssistantSidebar legacySessionId="meeting-a" />));
    expect(container.textContent).toContain(notice);
    expect(fake.command).not.toHaveBeenCalled();
    expect(fake.prepare).not.toHaveBeenCalled();
  });

  it("ignores late Host history after the caption session changes", async () => {
    let release!: (value: unknown) => void;
    fake.health.mockResolvedValue({ framework_enabled: false });
    fake.history.mockImplementationOnce(() => new Promise(resolve => { release = resolve; }));
    await act(async () => root?.render(<RoomAssistantSidebar legacySessionId="old" />));
    await act(async () => root?.render(<RoomAssistantSidebar legacySessionId="new" />));
    await act(async () => release([{ source_kind: "host_history", plugin_id: "old-meeting", name: "过期历史", media_session_id: "old-media", legacy_session_id: "old" }]));
    expect(container.textContent).not.toContain("过期历史"); expect(fake.hostActions).not.toHaveBeenCalled();
  });

  it("opens, switches plugins, folds and resizes without navigating or releasing tab audio", async () => {
    await startTabAudio();
    const center = container.querySelector(".studioCenter");
    const location = window.location.href;
    await click(button("助手工作区"));
    const panel = container.querySelector<HTMLElement>("#studio-panel-assistants")!;
    expect(panel.hidden).toBe(false);
    expect(panel.textContent).toContain("课程整理");
    const input = await typeNote("课程中的定义");
    await click(selectPlugin("plugin.generic"));
    expect(container.querySelector<HTMLInputElement>(".assistantDetail input")!.value).toBe("");
    await typeNote("另一助手的输入");
    await click(selectPlugin("plugin.course"));
    expect(container.querySelector<HTMLInputElement>(".assistantDetail input")!.value).toBe("课程中的定义");
    await click(button("直播空间"));
    expect(panel.hidden).toBe(true);
    await click(button("助手"));
    await click(button("收起侧栏"));
    expect(panel.hidden).toBe(true);
    expect(document.activeElement).toBe(button("助手"));
    await click(button("助手工作区"));
    await click(button("助手工作区"));
    expect(panel.hidden).toBe(false);
    expect(container.querySelector<HTMLInputElement>(".assistantDetail input")!.value).toBe(input.value);
    const separator = container.querySelector('[role="separator"]')!;
    await act(async () => { separator.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true })); });
    expect(separator.getAttribute("aria-valuenow")).toBe("408");
    expect(window.location.href).toBe(location);
    expect(fake.bridge).toHaveBeenCalledExactlyOnceWith("session-1");
    await act(async () => {
      fake.handlers.get("DataReceived")!(new TextEncoder().encode(JSON.stringify({
        schema_version: 1, topic: "caption", type: "caption.upsert", session_id: "session-1", sent_at_ms: 2000,
        payload: { segment_id: "segment-1", revision: 1, status: "final", text: "切换侧栏后继续收到字幕。", audio_start_ms: 0, audio_end_ms: 1500, confidence: null, provider_event_id: "event-1", received_at_ms: 2000 },
      })), undefined, undefined, "livecaption.events.v1");
    });
    expect(container.querySelector(".captionStage")!.textContent).toContain("切换侧栏后继续收到字幕。");
    assertMediaStillLive(center);
    await act(async () => root?.unmount());
    root = null;
    expect(fake.unpublish).toHaveBeenCalledExactlyOnceWith(fake.audio, true);
    expect(fake.audio.stop).toHaveBeenCalledOnce();
    expect(fake.video.stop).toHaveBeenCalledOnce();
    expect(fake.disconnect).toHaveBeenCalledOnce();
  });

  it("shows an honest pending state and separates history/management navigation", async () => {
    await mount();
    await click(button("助手工作区"));
    const panel = container.querySelector("#studio-panel-assistants")!;
    expect(panel.textContent).toContain("启动或选择字幕任务");
    expect(fake.bridge).not.toHaveBeenCalled();
    const links = [...panel.querySelectorAll("a")];
    expect(links.length).toBeGreaterThanOrEqual(2);
    for (const link of links) {
      expect(link.target).toBe("_blank");
      expect(link.rel).toBe("noopener noreferrer");
    }
  });

  it("keeps audio active when the plugin framework is disabled", async () => {
    fake.health.mockResolvedValue({ framework_enabled: false });
    await startTabAudio();
    const center = container.querySelector(".studioCenter");
    await click(button("助手工作区"));
    expect(container.querySelector("#studio-panel-assistants")!.textContent).toContain("插件框架已关闭");
    expect(fake.bridge).not.toHaveBeenCalled();
    assertMediaStillLive(center);
  });

  it("shows plugin errors without interrupting capture", async () => {
    fake.views.mockRejectedValue(new Error("测试视图暂不可用"));
    await startTabAudio();
    const center = container.querySelector(".studioCenter");
    await click(button("助手工作区"));
    expect(container.querySelector("#studio-panel-assistants")!.textContent).toContain("测试视图暂不可用");
    assertMediaStillLive(center);
  });

  it("keeps polling a hidden assistant without reconnecting or losing input", async () => {
    vi.useFakeTimers();
    await startTabAudio();
    await click(button("助手工作区"));
    await typeNote("未提交的笔记");
    const node = container.querySelector(".assistantDetail input");
    await click(button("直播空间"));
    const updated = view("plugin.course");
    updated.view_version = 2;
    updated.view.view_version = 2;
    updated.view.root.title = "实时笔记已刷新";
    fake.views.mockResolvedValue([updated, view("plugin.generic")]);
    await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
    await click(button("助手"));
    expect(container.querySelector(".assistantDetail input")).toBe(node);
    expect((node as HTMLInputElement).value).toBe("未提交的笔记");
    expect(container.querySelector(".assistantDetail")!.textContent).toContain("实时笔记已刷新");
    expect(fake.bridge).toHaveBeenCalledOnce();
    expect(fake.views.mock.calls.length).toBeGreaterThan(1);
    assertMediaStillLive(container.querySelector(".studioCenter"));
  });

  it("routes commands and failures only to the selected plugin and its values", async () => {
    await startTabAudio();
    await click(button("助手工作区"));
    await typeNote("课程命令内容");
    fake.command.mockRejectedValueOnce(new Error("仅课程命令失败"));
    await click(button("保存笔记"));
    expect(fake.command).toHaveBeenLastCalledWith("media-session-1", expect.objectContaining({
      plugin_id: "plugin.course", values: { note: "课程命令内容" }, action_id: "save",
    }));
    expect(container.querySelector(".assistantDetail")!.textContent).toContain("仅课程命令失败");
    await click(selectPlugin("plugin.generic"));
    expect(container.querySelector(".assistantDetail")!.textContent).not.toContain("仅课程命令失败");
    await typeNote("通用命令内容");
    await click(button("保存笔记"));
    expect(fake.command).toHaveBeenLastCalledWith("media-session-1", expect.objectContaining({
      plugin_id: "plugin.generic", values: { note: "通用命令内容" },
    }));
    assertMediaStillLive(container.querySelector(".studioCenter"));
  });

  it("keeps document exports scoped to the selected plugin and opens auxiliary links separately", async () => {
    fake.documents.mockResolvedValue([assistantDocument("plugin.course", "course-doc"), assistantDocument("plugin.generic", "generic-doc")]);
    await startTabAudio();
    await click(button("助手工作区"));
    const shelf = container.querySelector(".pluginDocumentShelf")!;
    expect(shelf.querySelectorAll('a[href*="course-doc"]')).toHaveLength(2);
    expect(shelf.querySelectorAll('a[href*="generic-doc"]')).toHaveLength(0);
    for (const link of container.querySelectorAll<HTMLAnchorElement>("#studio-panel-assistants a, .runExports a")) {
      expect(link.target).toBe("_blank");
      expect(link.rel).toBe("noopener noreferrer");
    }
    await click(selectPlugin("plugin.generic"));
    expect(container.querySelectorAll('.pluginDocumentShelf a[href*="generic-doc"]')).toHaveLength(2);
    expect(container.querySelectorAll('.pluginDocumentShelf a[href*="course-doc"]')).toHaveLength(0);
  });

  it("follows a changed caption session without carrying old input into the new scope", async () => {
    await act(async () => root?.render(<RoomAssistantSidebar legacySessionId="session-old" />));
    await typeNote("旧会话输入");
    await act(async () => root?.render(<RoomAssistantSidebar legacySessionId="session-new" />));
    expect(container.querySelector<HTMLInputElement>(".assistantDetail input")!.value).toBe("");
    expect(fake.bridge).toHaveBeenLastCalledWith("session-new");
    await typeNote("新会话输入");
    await click(button("保存笔记"));
    expect(fake.command).toHaveBeenLastCalledWith("media-session-new", expect.objectContaining({ values: { note: "新会话输入" } }));
  });
});
