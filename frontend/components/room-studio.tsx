"use client";

import {
  ConnectionState,
  LocalAudioTrack,
  Room,
  RoomEvent,
  Track,
  createLocalAudioTrack,
  createLocalScreenTracks,
} from "livekit-client";
import type {
  LocalTrack,
  Participant,
  TrackPublication,
} from "livekit-client";
import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type CSSProperties,
} from "react";

import {
  cancelCaptionRun,
  closeRoom,
  createCaptionRun,
  createRoom,
  createRoomToken,
  getSegments,
  getSession,
  getSessionRuntime,
  getSessionExportUrl,
  getTranslations,
  listCaptionRuns,
  listRooms,
  removeRoomParticipant,
  startHLSInput,
  stopHLSInput,
  updateRoom,
} from "@/lib/api";
import {
  createCaptionState,
  decodeLiveCaptionEvent,
  hydrateFinalSnapshot,
  hydrateTranslationSnapshot,
  reduceCaptionEvent,
  selectActiveDraftSegments,
  selectFinalSegments,
  selectTranslationDraftSegments,
  selectTranslationFinalSegments,
} from "@/lib/captions";
import { composeCaptionCues } from "@/lib/caption-cues";
import type { CaptionLayoutProfile } from "@/lib/caption-cues";
import { createCanvasTextMeasurer } from "@/lib/caption-measurement";
import { withTimeout } from "@/lib/async-timeout";
import { useElementWidth } from "@/hooks/use-element-width";
import { FloatingCaptions } from "@/components/floating-captions";
import { RoomAssistantLauncher } from "@/components/assistants/room-assistant-launcher";
import { RoomAssistantSidebar } from "@/components/assistants/room-assistant-sidebar";
import { StudioSidebar } from "@/components/assistants/studio-sidebar";
import { createStudioSidebarState, reduceStudioSidebar, SIDEBAR_RAIL_WIDTH } from "@/lib/studio-sidebar-state";
import { LIVE_CAPTION_TOPIC } from "@/types/captions";
import type { CaptionState } from "@/types/captions";
import type {
  CaptionInputType,
  CaptionRun,
  ManagedRoom,
} from "@/types/room";
import type { SessionRuntime, SessionStatus } from "@/types/session";

type ConnectionStatus =
  | "disconnected"
  | "connecting"
  | "connected"
  | "reconnecting"
  | "error";

type InputPhase =
  | "idle"
  | "acquiring"
  | "publishing"
  | "live"
  | "stopping"
  | "error";

type TrackView = {
  sid: string;
  name: string;
  kind: string;
  source: string;
  muted: boolean;
};

type ParticipantView = {
  identity: string;
  isLocal: boolean;
  tracks: TrackView[];
};

type ActiveInput = {
  sessionId: string;
  audioTrack: LocalAudioTrack;
  retainedTracks: LocalTrack[];
  audioElement: HTMLAudioElement | null;
  audioContext: AudioContext | null;
  objectUrl: string | null;
};

const TERMINAL_STATUSES = new Set<SessionStatus>([
  "completed",
  "failed",
  "cancelled",
]);
const TRACK_PUBLICATION_TIMEOUT_MS = 15_000;

const SOURCE_LABELS: Record<CaptionInputType, string> = {
  microphone: "浏览器麦克风",
  screen: "标签页 / 系统音频",
  file: "本地媒体文件",
  hls: "M3U8 直播流",
};

const STATUS_LABELS: Record<SessionStatus, string> = {
  created: "等待音轨",
  starting: "正在启动",
  running: "实时识别中",
  finalizing: "正在收尾",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

const LANGUAGE_OPTIONS = [
  ["zh-CN", "中文"],
  ["en-US", "English"],
  ["ja-JP", "日本語"],
  ["ko-KR", "한국어"],
  ["fr-FR", "Français"],
  ["de-DE", "Deutsch"],
  ["es-ES", "Español"],
  ["ru-RU", "Русский"],
  ["ar-SA", "العربية"],
] as const;

const TRANSLATION_STATUS_LABELS = {
  disabled: "未启用",
  starting: "连接翻译",
  running: "实时翻译中",
  completed: "翻译完成",
  failed: "翻译降级",
} as const;

function baseLanguage(value: string): string {
  return value.toLowerCase().split("-")[0] ?? value.toLowerCase();
}

function languageLabel(value: string | null): string {
  if (value === null) {
    return "未启用";
  }
  return (
    LANGUAGE_OPTIONS.find(([code]) => code === value)?.[1] ??
    value
  );
}

function captionReadingRate(language: string): number {
  return ["zh", "ja", "ko"].includes(baseLanguage(language)) ? 18 : 4.5;
}

function formatDate(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(parsed);
}

function runtimeFlag(value: boolean | null): string {
  if (value === null) {
    return "不可用";
  }
  return value ? "是" : "否";
}

function formatBytes(value: number | null): string {
  if (value === null) {
    return "不可用";
  }
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KiB`;
  }
  return `${(value / 1024 / 1024).toFixed(2)} MiB`;
}

function formatAudioTime(milliseconds: number | null): string {
  if (milliseconds === null) {
    return "--:--.---";
  }
  const safe = Math.max(0, Math.round(milliseconds));
  const minutes = Math.floor(safe / 60_000);
  const seconds = Math.floor((safe % 60_000) / 1_000);
  const millis = safe % 1_000;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(millis).padStart(3, "0")}`;
}

function publicationView(publication: TrackPublication): TrackView {
  return {
    sid: publication.trackSid,
    name: publication.trackName || "未命名轨道",
    kind: publication.kind,
    source: publication.source,
    muted: publication.isMuted,
  };
}

function participantView(
  participant: Participant,
  isLocal: boolean,
): ParticipantView {
  return {
    identity: participant.identity,
    isLocal,
    tracks: Array.from(participant.trackPublications.values())
      .map(publicationView)
      .sort((left, right) => left.name.localeCompare(right.name)),
  };
}

async function releaseInput(room: Room | null, input: ActiveInput): Promise<void> {
  if (input.audioElement !== null) {
    input.audioElement.onended = null;
    input.audioElement.pause();
    input.audioElement.src = "";
  }
  if (room !== null && room.state === ConnectionState.Connected) {
    try {
      await room.localParticipant.unpublishTrack(input.audioTrack, true);
    } catch {
      input.audioTrack.stop();
    }
  } else {
    input.audioTrack.stop();
  }
  for (const track of input.retainedTracks) {
    if (track !== input.audioTrack) {
      track.stop();
    }
  }
  if (input.audioContext !== null && input.audioContext.state !== "closed") {
    await input.audioContext.close();
  }
  if (input.objectUrl !== null) {
    URL.revokeObjectURL(input.objectUrl);
  }
}

export function RoomStudio() {
  const [sidebar, dispatchSidebar] = useReducer(reduceStudioSidebar, undefined, createStudioSidebarState);
  const roomRef = useRef<Room | null>(null);
  const activeInputRef = useRef<ActiveInput | null>(null);
  const activeHlsSessionRef = useRef<string | null>(null);
  const activeSessionIdRef = useRef<string | null>(null);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const selectedRoomRef = useRef<ManagedRoom | null>(null);
  const sourceCaptionLaneRef = useRef<HTMLElement | null>(null);
  const translationCaptionLaneRef = useRef<HTMLElement | null>(null);

  const [rooms, setRooms] = useState<ManagedRoom[]>([]);
  const [selectedRoom, setSelectedRoom] = useState<ManagedRoom | null>(null);
  const [newRoomName, setNewRoomName] = useState("调试直播间");
  const [connectionStatus, setConnectionStatus] =
    useState<ConnectionStatus>("disconnected");
  const [participants, setParticipants] = useState<ParticipantView[]>([]);
  const [runs, setRuns] = useState<CaptionRun[]>([]);
  const [activeRun, setActiveRun] = useState<CaptionRun | null>(null);
  const [captionState, setCaptionState] = useState<CaptionState>(() =>
    createCaptionState(),
  );
  const [sourceType, setSourceType] =
    useState<CaptionInputType>("microphone");
  const [language, setLanguage] = useState("zh-CN");
  const [targetLanguage, setTargetLanguage] = useState("en-US");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [hlsUrl, setHlsUrl] = useState("");
  const [inputPhase, setInputPhase] = useState<InputPhase>("idle");
  const [error, setError] = useState<string | null>(null);
  const [roomsLoading, setRoomsLoading] = useState(false);
  const [runtimeSnapshot, setRuntimeSnapshot] =
    useState<SessionRuntime | null>(null);
  const [runtimeError, setRuntimeError] = useState<string | null>(null);
  const sourceLaneWidth = useElementWidth(sourceCaptionLaneRef, 520);
  const translationLaneWidth = useElementWidth(
    translationCaptionLaneRef,
    520,
  );

  const drafts = useMemo(
    () => selectActiveDraftSegments(captionState),
    [captionState],
  );
  const finals = useMemo(
    () => selectFinalSegments(captionState),
    [captionState],
  );
  const translationDrafts = useMemo(
    () => selectTranslationDraftSegments(captionState),
    [captionState],
  );
  const translationFinals = useMemo(
    () => selectTranslationFinalSegments(captionState),
    [captionState],
  );
  const displaySourceLanguage = activeRun?.language ?? language;
  const visibleTargetLanguage =
    activeRun?.target_language ?? (targetLanguage || null);
  const displayTargetLanguage =
    visibleTargetLanguage ?? (targetLanguage || "en-US");
  const sourceTextMeasurer = useMemo(
    () => createCanvasTextMeasurer(
      '13px Inter, ui-sans-serif, system-ui, "Segoe UI", sans-serif',
    ),
    [],
  );
  const translationTextMeasurer = useMemo(
    () => createCanvasTextMeasurer(
      '13px Inter, ui-sans-serif, system-ui, "Segoe UI", sans-serif',
    ),
    [],
  );
  const sourceLayoutProfile = useMemo<CaptionLayoutProfile>(() => ({
    maxLineWidthPx: Math.max(120, sourceLaneWidth - 160),
    maxLines: 2,
    minFillRatio: 0.3,
    maxReadingUnitsPerSecond: captionReadingRate(displaySourceLanguage),
    measureText: sourceTextMeasurer,
  }), [displaySourceLanguage, sourceLaneWidth, sourceTextMeasurer]);
  const translationLayoutProfile = useMemo<CaptionLayoutProfile>(() => ({
    maxLineWidthPx: Math.max(120, translationLaneWidth - 160),
    maxLines: 2,
    minFillRatio: 0.3,
    maxReadingUnitsPerSecond: captionReadingRate(displayTargetLanguage),
    measureText: translationTextMeasurer,
  }), [displayTargetLanguage, translationLaneWidth, translationTextMeasurer]);
  const sourceConfidenceBySegment = useMemo(
    () => new Map(finals.map((caption) => [
      caption.segment_id,
      caption.confidence,
    ])),
    [finals],
  );
  const sourceDisplayDrafts = useMemo(
    () => drafts.flatMap((caption) => {
      const cues = composeCaptionCues([{
        segmentId: caption.segment_id,
        revision: caption.revision,
        status: caption.status,
        text: caption.text,
        language: displaySourceLanguage,
        audioStartMs: caption.audio_start_ms,
        audioEndMs: caption.audio_end_ms,
      }], sourceLayoutProfile);
      const latest = cues.at(-1);
      return latest === undefined ? [] : [latest];
    }),
    [drafts, displaySourceLanguage, sourceLayoutProfile],
  );
  const sourceDisplayFinals = useMemo(
    () => composeCaptionCues(finals.map((caption) => ({
      segmentId: caption.segment_id,
      revision: caption.revision,
      status: caption.status,
      text: caption.text,
      language: displaySourceLanguage,
      audioStartMs: caption.audio_start_ms,
      audioEndMs: caption.audio_end_ms,
    })), sourceLayoutProfile),
    [displaySourceLanguage, finals, sourceLayoutProfile],
  );
  const translationDisplayDrafts = useMemo(
    () => translationDrafts.flatMap((caption) => {
      const cues = composeCaptionCues([{
        segmentId: caption.segment_id,
        revision: caption.revision,
        status: caption.status,
        text: caption.text,
        language: caption.target_language || displayTargetLanguage,
        audioStartMs: caption.audio_start_ms,
        audioEndMs: caption.audio_end_ms,
      }], translationLayoutProfile);
      const latest = cues.at(-1);
      return latest === undefined ? [] : [latest];
    }),
    [displayTargetLanguage, translationDrafts, translationLayoutProfile],
  );
  const translationDisplayFinals = useMemo(
    () => composeCaptionCues(translationFinals.map((caption) => ({
      segmentId: caption.segment_id,
      revision: caption.revision,
      status: caption.status,
      text: caption.text,
      language: caption.target_language || displayTargetLanguage,
      audioStartMs: caption.audio_start_ms,
      audioEndMs: caption.audio_end_ms,
    })), translationLayoutProfile),
    [displayTargetLanguage, translationFinals, translationLayoutProfile],
  );

  const syncParticipants = useCallback((room: Room) => {
    const next = [
      participantView(room.localParticipant, true),
      ...Array.from(room.remoteParticipants.values()).map((participant) =>
        participantView(participant, false),
      ),
    ].sort((left, right) => {
      if (left.isLocal !== right.isLocal) {
        return left.isLocal ? -1 : 1;
      }
      return left.identity.localeCompare(right.identity);
    });
    setParticipants(next);
  }, []);

  const refreshRooms = useCallback(async () => {
    setRoomsLoading(true);
    try {
      const next = await listRooms();
      setRooms(next);
      setSelectedRoom((current) => {
        if (current === null) {
          return current;
        }
        const refreshed =
          next.find((candidate) => candidate.id === current.id) ?? null;
        selectedRoomRef.current = refreshed;
        return refreshed;
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Room 列表加载失败");
    } finally {
      setRoomsLoading(false);
    }
  }, []);

  const refreshRuns = useCallback(async (roomId: string) => {
    const next = await listCaptionRuns(roomId);
    setRuns(next);
    return next;
  }, []);

  const loadRun = useCallback(async (run: CaptionRun) => {
    const [snapshot, translationSnapshot] = await Promise.all([
      getSegments(run.id),
      getTranslations(run.id),
    ]);
    activeSessionIdRef.current = run.id;
    setActiveRun(run);
    setLanguage(run.language);
    setTargetLanguage(run.target_language ?? "");
    setCaptionState(
      hydrateTranslationSnapshot(
        hydrateFinalSnapshot(
          createCaptionState(run.status, run.translation_status),
          snapshot,
        ),
        translationSnapshot,
      ),
    );
  }, []);

  const monitorHLSRun = useCallback(
    async (roomId: string, sessionId: string) => {
      if (selectedRoomRef.current?.id !== roomId) {
        return;
      }
      try {
        const refreshed = (await getSession(sessionId)) as CaptionRun;
        setActiveRun(refreshed);
        setCaptionState((current) => ({
          ...current,
          sessionStatus: refreshed.status,
          error: current.error ?? refreshed.error_message,
          translationStatus: refreshed.translation_status,
          translationError:
            current.translationError ??
            refreshed.translation_error_message,
        }));
        if (TERMINAL_STATUSES.has(refreshed.status)) {
          activeHlsSessionRef.current = null;
          const [snapshot, translationSnapshot] = await Promise.all([
            getSegments(sessionId),
            getTranslations(sessionId),
            refreshRuns(roomId),
          ]);
          setCaptionState((current) =>
            hydrateTranslationSnapshot(
              hydrateFinalSnapshot(current, snapshot),
              translationSnapshot,
            ),
          );
          setInputPhase("idle");
          pollTimerRef.current = null;
          return;
        }
        pollTimerRef.current = setTimeout(() => {
          void monitorHLSRun(roomId, sessionId);
        }, 1_500);
      } catch (caught) {
        setError(
          caught instanceof Error ? caught.message : "直播流状态刷新失败",
        );
        setInputPhase("error");
        pollTimerRef.current = null;
      }
    },
    [refreshRuns],
  );

  const disconnectCurrentRoom = useCallback(async () => {
    if (pollTimerRef.current !== null) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    const input = activeInputRef.current;
    activeInputRef.current = null;
    activeHlsSessionRef.current = null;
    const room = roomRef.current;
    if (input !== null) {
      await releaseInput(room, input);
    }
    roomRef.current = null;
    if (room !== null) {
      room.removeAllListeners();
      await room.disconnect();
    }
    setInputPhase("idle");
    setParticipants([]);
    setConnectionStatus("disconnected");
  }, []);

  const openRoom = useCallback(
    async (target: ManagedRoom) => {
      if (target.status === "closed") {
        return;
      }
      await disconnectCurrentRoom();
      selectedRoomRef.current = target;
      setSelectedRoom(target);
      setConnectionStatus("connecting");
      setError(null);
      setRuns([]);
      setActiveRun(null);
      activeHlsSessionRef.current = null;
      activeSessionIdRef.current = null;
      setCaptionState(createCaptionState());

      const room = new Room({ adaptiveStream: true, dynacast: true });
      roomRef.current = room;

      const refreshParticipantState = () => syncParticipants(room);
      room.on(RoomEvent.Reconnecting, () =>
        setConnectionStatus("reconnecting"),
      );
      room.on(RoomEvent.Reconnected, () => {
        setConnectionStatus("connected");
        syncParticipants(room);
      });
      room.on(RoomEvent.Disconnected, () => {
        if (roomRef.current === room) {
          roomRef.current = null;
          setConnectionStatus("disconnected");
          setParticipants([]);
        }
      });
      room.on(RoomEvent.ParticipantConnected, refreshParticipantState);
      room.on(RoomEvent.ParticipantDisconnected, refreshParticipantState);
      room.on(RoomEvent.TrackPublished, refreshParticipantState);
      room.on(RoomEvent.TrackUnpublished, refreshParticipantState);
      room.on(RoomEvent.TrackSubscribed, refreshParticipantState);
      room.on(RoomEvent.TrackUnsubscribed, refreshParticipantState);
      room.on(RoomEvent.LocalTrackPublished, refreshParticipantState);
      room.on(RoomEvent.LocalTrackUnpublished, refreshParticipantState);
      room.on(RoomEvent.TrackMuted, refreshParticipantState);
      room.on(RoomEvent.TrackUnmuted, refreshParticipantState);
      room.on(RoomEvent.DataReceived, (payload, _participant, _kind, topic) => {
        if (topic !== LIVE_CAPTION_TOPIC) {
          return;
        }
        const sessionId = activeSessionIdRef.current;
        if (sessionId === null) {
          return;
        }
        const event = decodeLiveCaptionEvent(payload, sessionId);
        if (event === null) {
          return;
        }
        setCaptionState((current) => reduceCaptionEvent(current, event));
        if (event.type === "session.status") {
          setActiveRun((current) =>
            current === null
              ? current
              : { ...current, status: event.payload.status },
          );
          if (TERMINAL_STATUSES.has(event.payload.status)) {
            activeHlsSessionRef.current = null;
            if (pollTimerRef.current !== null) {
              clearTimeout(pollTimerRef.current);
              pollTimerRef.current = null;
            }
            setInputPhase("idle");
            void refreshRuns(target.id);
          }
        } else if (event.type === "translation.status") {
          setActiveRun((current) =>
            current === null
              ? current
              : {
                  ...current,
                  translation_status: event.payload.status,
                  translation_error_code: event.payload.error_code,
                  translation_error_message: event.payload.message,
                },
          );
        }
      });

      try {
        const [token, roomRuns] = await Promise.all([
          createRoomToken(target.id),
          refreshRuns(target.id),
        ]);
        await room.connect(token.url, token.token);
        if (roomRef.current !== room) {
          await room.disconnect();
          return;
        }
        setConnectionStatus("connected");
        syncParticipants(room);
        const latest = roomRuns[0];
        if (latest !== undefined) {
          await loadRun(latest);
          if (
            latest.source_type === "hls" &&
            !TERMINAL_STATUSES.has(latest.status)
          ) {
            activeHlsSessionRef.current = latest.id;
            setSourceType("hls");
            setHlsUrl(latest.source_name);
            setInputPhase("live");
            void monitorHLSRun(target.id, latest.id);
          }
        }
      } catch (caught) {
        room.removeAllListeners();
        await room.disconnect();
        if (roomRef.current === room) {
          roomRef.current = null;
        }
        setConnectionStatus("error");
        setError(
          caught instanceof Error ? caught.message : "连接 Room 失败",
        );
      }
    },
    [
      disconnectCurrentRoom,
      loadRun,
      monitorHLSRun,
      refreshRuns,
      syncParticipants,
    ],
  );

  const pollRunUntilTerminal = useCallback(
    async (roomId: string, sessionId: string, attempts = 0) => {
      try {
        const refreshed = (await getSession(sessionId)) as CaptionRun;
        setActiveRun(refreshed);
        setCaptionState((current) => ({
          ...current,
          sessionStatus: refreshed.status,
          error: current.error ?? refreshed.error_message,
          translationStatus: refreshed.translation_status,
          translationError:
            current.translationError ??
            refreshed.translation_error_message,
        }));
        if (TERMINAL_STATUSES.has(refreshed.status) || attempts >= 18) {
          if (activeHlsSessionRef.current === sessionId) {
            activeHlsSessionRef.current = null;
          }
          const [snapshot, translationSnapshot] = await Promise.all([
            getSegments(sessionId),
            getTranslations(sessionId),
            refreshRuns(roomId),
          ]);
          setCaptionState((current) =>
            hydrateTranslationSnapshot(
              hydrateFinalSnapshot(current, snapshot),
              translationSnapshot,
            ),
          );
          setInputPhase("idle");
          pollTimerRef.current = null;
          return;
        }
        pollTimerRef.current = setTimeout(() => {
          void pollRunUntilTerminal(roomId, sessionId, attempts + 1);
        }, 750);
      } catch (caught) {
        setError(
          caught instanceof Error ? caught.message : "字幕任务状态刷新失败",
        );
        setInputPhase("error");
      }
    },
    [refreshRuns],
  );

  const stopInput = useCallback(async () => {
    const input = activeInputRef.current;
    const hlsSessionId = activeHlsSessionRef.current;
    const run = activeRun;
    const canCancelWaitingRun =
      run !== null && !TERMINAL_STATUSES.has(run.status);
    if (
      input === null &&
      hlsSessionId === null &&
      !canCancelWaitingRun
    ) {
      return;
    }
    if (pollTimerRef.current !== null) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    activeInputRef.current = null;
    setInputPhase("stopping");
    try {
      const room = selectedRoomRef.current;
      if (hlsSessionId !== null) {
        if (room === null) {
          throw new Error("当前 Room 已断开，无法停止直播流");
        }
        await stopHLSInput(room.id, hlsSessionId);
        activeHlsSessionRef.current = null;
        void pollRunUntilTerminal(room.id, hlsSessionId);
        return;
      }
      if (input === null) {
        if (room === null || run === null) {
          throw new Error("当前 Room 已断开，无法取消等待任务");
        }
        const cancelled = await cancelCaptionRun(room.id, run.id);
        setActiveRun(cancelled);
        setCaptionState((current) => ({
          ...current,
          sessionStatus: cancelled.status,
          translationStatus: cancelled.translation_status,
        }));
        await refreshRuns(room.id);
        setInputPhase("idle");
        return;
      }
      await releaseInput(roomRef.current, input);
      if (room !== null) {
        void pollRunUntilTerminal(room.id, input.sessionId);
      } else {
        setInputPhase("idle");
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "停止输入失败");
      setInputPhase(hlsSessionId === null ? "error" : "live");
    }
  }, [activeRun, pollRunUntilTerminal, refreshRuns]);

  const startInput = useCallback(async () => {
    const room = roomRef.current;
    const managedRoom = selectedRoomRef.current;
    if (
      room === null ||
      managedRoom === null ||
      room.state !== ConnectionState.Connected
    ) {
      setError("请先进入一个已连接的 Room");
      return;
    }
    if (
      activeInputRef.current !== null ||
      activeHlsSessionRef.current !== null
    ) {
      return;
    }
    if (sourceType === "file" && selectedFile === null) {
      setError("请先选择本地音频或视频文件");
      return;
    }
    if (sourceType === "hls") {
      const candidate = hlsUrl.trim();
      try {
        const parsed = new URL(candidate);
        if (!["http:", "https:"].includes(parsed.protocol)) {
          throw new Error("unsupported protocol");
        }
      } catch {
        setError("请输入有效的 HTTP/HTTPS M3U8 地址");
        return;
      }
    }

    setError(null);
    setInputPhase("acquiring");
    let audioTrack: LocalAudioTrack | null = null;
    let retainedTracks: LocalTrack[] = [];
    let audioElement: HTMLAudioElement | null = null;
    let audioContext: AudioContext | null = null;
    let objectUrl: string | null = null;
    let createdRun: CaptionRun | null = null;

    try {
      if (sourceType === "hls") {
        createdRun = await startHLSInput(managedRoom.id, {
          url: hlsUrl.trim(),
          language: language.trim() || "zh-CN",
          target_language: targetLanguage || null,
        });
        activeHlsSessionRef.current = createdRun.id;
        activeSessionIdRef.current = createdRun.id;
        setActiveRun(createdRun);
        setCaptionState(
          createCaptionState(
            createdRun.status,
            createdRun.translation_status,
          ),
        );
        setRuns((current) => [
          createdRun as CaptionRun,
          ...current.filter((run) => run.id !== createdRun?.id),
        ]);
        setInputPhase("live");
        void monitorHLSRun(managedRoom.id, createdRun.id);
        return;
      }
      if (sourceType === "microphone") {
        audioTrack = await createLocalAudioTrack({
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        });
      } else if (sourceType === "screen") {
        // Prefer another tab/window (e.g. a course page), not this Studio tab.
        // preferCurrentTab + selfBrowserSurface:"exclude" is contradictory and
        // Chrome rejects getDisplayMedia with that combination.
        retainedTracks = await createLocalScreenTracks({
          audio: true,
          video: true,
          systemAudio: "include",
          selfBrowserSurface: "exclude",
        });
        const capturedAudio = retainedTracks.find(
          (track): track is LocalAudioTrack =>
            track.kind === Track.Kind.Audio,
        );
        if (capturedAudio === undefined) {
          throw new Error(
            "所选窗口没有提供音频。请选择浏览器标签页，并勾选“共享标签页音频”。",
          );
        }
        audioTrack = capturedAudio;
      } else {
        const file = selectedFile;
        if (file === null) {
          throw new Error("请选择本地媒体文件");
        }
        objectUrl = URL.createObjectURL(file);
        audioElement = new Audio(objectUrl);
        audioElement.preload = "auto";
        audioContext = new AudioContext();
        await audioContext.resume();
        const source = audioContext.createMediaElementSource(audioElement);
        const destination = audioContext.createMediaStreamDestination();
        source.connect(destination);
        source.connect(audioContext.destination);
        const mediaTrack = destination.stream.getAudioTracks()[0];
        if (mediaTrack === undefined) {
          throw new Error("浏览器未能从该文件创建音频轨道");
        }
        audioTrack = new LocalAudioTrack(
          mediaTrack,
          undefined,
          true,
          audioContext,
        );
      }

      const sourceName =
        sourceType === "file" && selectedFile !== null
          ? selectedFile.name
          : SOURCE_LABELS[sourceType];
      createdRun = await createCaptionRun(managedRoom.id, {
        source_type: sourceType,
        source_name: sourceName,
        language: language.trim() || "zh-CN",
        target_language: targetLanguage || null,
      });
      activeSessionIdRef.current = createdRun.id;
      setActiveRun(createdRun);
      setCaptionState(
        createCaptionState(
          createdRun.status,
          createdRun.translation_status,
        ),
      );
      setRuns((current) => [
        createdRun as CaptionRun,
        ...current.filter((run) => run.id !== createdRun?.id),
      ]);

      setInputPhase("publishing");
      await withTimeout(
        room.localParticipant.publishTrack(audioTrack, {
          name: `caption-input-${createdRun.id}`,
          source:
            sourceType === "screen"
              ? Track.Source.ScreenShareAudio
              : Track.Source.Microphone,
        }),
        TRACK_PUBLICATION_TIMEOUT_MS,
        "音轨发布超时，任务已自动取消。请确认 Room 仍已连接后重试。",
      );
      const activeInput: ActiveInput = {
        sessionId: createdRun.id,
        audioTrack,
        retainedTracks,
        audioElement,
        audioContext,
        objectUrl,
      };
      activeInputRef.current = activeInput;
      if (audioElement !== null) {
        audioElement.onended = () => {
          void stopInput();
        };
        await audioElement.play();
      }
      syncParticipants(room);
      setInputPhase("live");
    } catch (caught) {
      if (createdRun !== null) {
        try {
          const cancelled = await cancelCaptionRun(
            managedRoom.id,
            createdRun.id,
          );
          setActiveRun(cancelled);
          setCaptionState((current) => ({
            ...current,
            sessionStatus: cancelled.status,
            translationStatus: cancelled.translation_status,
          }));
          await refreshRuns(managedRoom.id);
        } catch {
          // Preserve the media/publication error as the primary message.
        }
      }
      if (audioTrack !== null) {
        await releaseInput(room, {
          sessionId: createdRun?.id ?? "",
          audioTrack,
          retainedTracks,
          audioElement,
          audioContext,
          objectUrl,
        });
      } else {
        for (const track of retainedTracks) {
          track.stop();
        }
        if (audioContext !== null && audioContext.state !== "closed") {
          await audioContext.close();
        }
        if (objectUrl !== null) {
          URL.revokeObjectURL(objectUrl);
        }
      }
      activeInputRef.current = null;
      setInputPhase("error");
      setError(
        caught instanceof Error ? caught.message : "启动音频输入失败",
      );
    }
  }, [
    hlsUrl,
    language,
    monitorHLSRun,
    refreshRuns,
    selectedFile,
    sourceType,
    stopInput,
    syncParticipants,
    targetLanguage,
  ]);

  const createNewRoom = useCallback(async () => {
    const name = newRoomName.trim();
    if (!name) {
      setError("请输入 Room 名称");
      return;
    }
    setRoomsLoading(true);
    setError(null);
    try {
      const created = await createRoom(name);
      setRooms((current) => [created, ...current]);
      await openRoom(created);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "创建 Room 失败");
    } finally {
      setRoomsLoading(false);
    }
  }, [newRoomName, openRoom]);

  const renameSelectedRoom = useCallback(async () => {
    const current = selectedRoomRef.current;
    if (current === null) {
      return;
    }
    const nextName = window.prompt("新的 Room 显示名称", current.display_name);
    if (nextName === null || !nextName.trim()) {
      return;
    }
    try {
      const updated = await updateRoom(current.id, nextName.trim());
      selectedRoomRef.current = updated;
      setSelectedRoom(updated);
      setRooms((items) =>
        items.map((item) => (item.id === updated.id ? updated : item)),
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "重命名失败");
    }
  }, []);

  const closeSelectedRoom = useCallback(async () => {
    const current = selectedRoomRef.current;
    if (current === null) {
      return;
    }
    if (!window.confirm(`关闭 Room“${current.display_name}”？`)) {
      return;
    }
    setError(null);
    try {
      await disconnectCurrentRoom();
      const closed = await closeRoom(current.id);
      selectedRoomRef.current = closed;
      setSelectedRoom(closed);
      setRooms((items) =>
        items.map((item) => (item.id === closed.id ? closed : item)),
      );
      setRuns([]);
      setActiveRun(null);
      activeSessionIdRef.current = null;
      setCaptionState(createCaptionState());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "关闭 Room 失败");
    }
  }, [disconnectCurrentRoom]);

  const removeParticipant = useCallback(async (identity: string) => {
    const current = selectedRoomRef.current;
    if (current === null) {
      return;
    }
    try {
      await removeRoomParticipant(current.id, identity);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "移除参与者失败");
    }
  }, []);

  const inspectRun = useCallback(
    async (run: CaptionRun) => {
      if (
        activeInputRef.current !== null ||
        activeHlsSessionRef.current !== null
      ) {
        return;
      }
      try {
        await loadRun(run);
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : "任务加载失败");
      }
    },
    [loadRun],
  );

  useEffect(() => {
    void refreshRooms();
  }, [refreshRooms]);

  useEffect(() => {
    const run = activeRun;
    if (run === null) {
      setRuntimeSnapshot(null);
      setRuntimeError(null);
      return;
    }
    const sessionId = run.id;
    let disposed = false;
    const refreshRuntime = async () => {
      try {
        const snapshot = await getSessionRuntime(sessionId);
        if (!disposed) {
          setRuntimeSnapshot(snapshot);
          setRuntimeError(null);
          setActiveRun((current) =>
            current?.id === sessionId
              ? { ...current, status: snapshot.session_status }
              : current,
          );
          setCaptionState((current) => ({
            ...current,
            sessionStatus: snapshot.session_status,
          }));
        }
      } catch (caught) {
        if (!disposed) {
          setRuntimeError(
            caught instanceof Error ? caught.message : "运行诊断加载失败",
          );
        }
      }
    };
    void refreshRuntime();
    const timer = TERMINAL_STATUSES.has(run.status)
      ? null
      : window.setInterval(() => void refreshRuntime(), 2_000);
    return () => {
      disposed = true;
      if (timer !== null) {
        window.clearInterval(timer);
      }
    };
  }, [activeRun?.id, activeRun?.status]);

  useEffect(() => {
    return () => {
      if (pollTimerRef.current !== null) {
        clearTimeout(pollTimerRef.current);
      }
      const input = activeInputRef.current;
      const room = roomRef.current;
      activeInputRef.current = null;
      activeHlsSessionRef.current = null;
      roomRef.current = null;
      if (input !== null) {
        void releaseInput(room, input);
      }
      if (room !== null) {
        room.removeAllListeners();
        void room.disconnect();
      }
    };
  }, []);

  const inputBusy =
    inputPhase === "acquiring" ||
    inputPhase === "publishing" ||
    inputPhase === "stopping";
  const canStart =
    selectedRoom?.status === "ready" &&
    connectionStatus === "connected" &&
    inputPhase !== "live" &&
    !inputBusy &&
    (targetLanguage === "" ||
      baseLanguage(targetLanguage) !== baseLanguage(language)) &&
    (activeRun === null || TERMINAL_STATUSES.has(activeRun.status));
  const canStop =
    inputPhase === "live" ||
    (!inputBusy &&
      activeRun !== null &&
      !TERMINAL_STATUSES.has(activeRun.status));
  return (
    <main className="studioShell">
      <header className="studioHeader">
        <div>
          <p className="eyebrow">LiveCaption Studio · Debug Console</p>
          <h1>直播字幕控制台</h1>
          <p>
            管理 Room、接入实时音频，并在同一工作区观察参与者、轨道与字幕台本。
          </p>
        </div>
        <div className="studioHeaderActions">
          <RoomAssistantLauncher expanded={sidebar.tab === "assistants" && !sidebar.collapsed}
            onOpen={() => dispatchSidebar({ type: "open", tab: "assistants" })} />
          <div className={`connectionBadge connection-${connectionStatus}`}>
            <span />
            {connectionStatus === "connected"
              ? "Room 已连接"
              : connectionStatus === "connecting"
                ? "正在连接"
                : connectionStatus === "reconnecting"
                  ? "正在重连"
                  : connectionStatus === "error"
                    ? "连接异常"
                    : "未连接"}
          </div>
        </div>
      </header>

      {error !== null ? (
        <div className="studioError" role="alert">
          <strong>操作未完成</strong>
          <span>{error}</span>
          <button type="button" onClick={() => setError(null)}>
            关闭
          </button>
        </div>
      ) : null}

      <div className="studioGrid studioGridWithSidebar" style={{
        "--studio-sidebar-width": `${sidebar.collapsed ? SIDEBAR_RAIL_WIDTH : sidebar.width}px`,
        "--studio-sidebar-expanded-width": `${sidebar.width}px`,
      } as CSSProperties}>
        <StudioSidebar state={sidebar} onAction={dispatchSidebar}
          assistants={<RoomAssistantSidebar legacySessionId={activeRun?.id ?? null} />}
          rooms={<section className="studioPanel roomSidebar">
          <div className="panelHeading">
            <div>
              <span className="label">Rooms</span>
              <h2>直播空间</h2>
            </div>
            <button
              type="button"
              className="iconButton"
              onClick={() => void refreshRooms()}
              disabled={roomsLoading}
              aria-label="刷新 Room"
            >
              ↻
            </button>
          </div>

          <div className="newRoomForm">
            <input
              value={newRoomName}
              onChange={(event) => setNewRoomName(event.target.value)}
              placeholder="Room 显示名称"
              maxLength={255}
            />
            <button
              type="button"
              onClick={() => void createNewRoom()}
              disabled={roomsLoading}
            >
              + 新建 Room
            </button>
          </div>

          <div className="roomList">
            {rooms.length === 0 && !roomsLoading ? (
              <p className="emptyState">还没有 Room，先创建一个调试直播间。</p>
            ) : null}
            {rooms.map((room) => (
              <button
                type="button"
                key={room.id}
                className={
                  selectedRoom?.id === room.id
                    ? "roomListItem active"
                    : "roomListItem"
                }
                onClick={() => void openRoom(room)}
                disabled={room.status === "closed"}
              >
                <span className="roomListTop">
                  <strong>{room.display_name}</strong>
                  <i className={`roomState roomState-${room.status}`}>
                    {room.status === "ready" ? "可用" : "已关闭"}
                  </i>
                </span>
                <code>{room.room_name}</code>
                <small>{formatDate(room.updated_at)}</small>
              </button>
            ))}
          </div>
        </section>} />

        <section className="studioCenter">
          <section className="studioPanel roomToolbar">
            {selectedRoom === null ? (
              <div className="roomPlaceholder">
                <span>01</span>
                <div>
                  <h2>选择或创建 Room</h2>
                  <p>进入 Room 后，输入流控制和实时字幕会出现在这里。</p>
                </div>
              </div>
            ) : (
              <>
                <div>
                  <span className="label">当前 Room</span>
                  <h2>{selectedRoom.display_name}</h2>
                  <code>{selectedRoom.room_name}</code>
                </div>
                <div className="toolbarActions">
                  <button
                    type="button"
                    className="secondary"
                    onClick={() => void renameSelectedRoom()}
                    disabled={selectedRoom.status === "closed"}
                  >
                    重命名
                  </button>
                  <button
                    type="button"
                    className="dangerButton"
                    onClick={() => void closeSelectedRoom()}
                    disabled={selectedRoom.status === "closed"}
                  >
                    关闭 Room
                  </button>
                </div>
              </>
            )}
          </section>

          <section className="studioPanel inputConsole">
            <div className="panelHeading">
              <div>
                <span className="label">Input source</span>
                <h2>接入音频流</h2>
              </div>
              <span className={`inputPhase inputPhase-${inputPhase}`}>
                {inputPhase === "live"
                  ? "采集中"
                  : inputPhase === "acquiring"
                    ? "等待授权"
                    : inputPhase === "publishing"
                      ? "发布音轨"
                      : inputPhase === "stopping"
                        ? "任务收尾"
                        : inputPhase === "error"
                          ? "需要处理"
                          : "待机"}
              </span>
            </div>

            <div className="sourceCards">
              {(
                [
                  "microphone",
                  "screen",
                  "file",
                  "hls",
                ] as CaptionInputType[]
              ).map((source) => (
                <button
                  type="button"
                  key={source}
                  className={
                    sourceType === source
                      ? "sourceCard active"
                      : "sourceCard"
                  }
                  onClick={() => setSourceType(source)}
                  disabled={inputPhase === "live" || inputBusy}
                >
                  <span aria-hidden="true">
                    {source === "microphone"
                      ? "MIC"
                      : source === "screen"
                        ? "TAB"
                        : source === "file"
                          ? "FILE"
                          : "HLS"}
                  </span>
                  <strong>{SOURCE_LABELS[source]}</strong>
                  <small>
                    {source === "microphone"
                      ? "适合现场讲话和外接声卡"
                      : source === "screen"
                        ? "捕获浏览器标签页或系统共享音频"
                        : source === "file"
                          ? "直接播放本地音视频文件进行调试"
                          : "由服务器拉取公开 M3U8 中的音频"}
                  </small>
                </button>
              ))}
            </div>

            <div className="inputOptions">
              <label>
                <span>源语种</span>
                <select
                  value={language}
                  onChange={(event) => {
                    const next = event.target.value;
                    setLanguage(next);
                    if (
                      targetLanguage !== "" &&
                      baseLanguage(next) === baseLanguage(targetLanguage)
                    ) {
                      setTargetLanguage("");
                    }
                  }}
                  disabled={inputPhase === "live" || inputBusy}
                >
                  {LANGUAGE_OPTIONS.map(([code, label]) => (
                    <option key={code} value={code}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <span>翻译字幕</span>
                <select
                  value={targetLanguage}
                  onChange={(event) =>
                    setTargetLanguage(event.target.value)
                  }
                  disabled={inputPhase === "live" || inputBusy}
                >
                  <option value="">不启用翻译</option>
                  {LANGUAGE_OPTIONS.map(([code, label]) => (
                    <option
                      key={code}
                      value={code}
                      disabled={
                        baseLanguage(code) === baseLanguage(language)
                      }
                    >
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              {sourceType === "file" ? (
                <label className="filePicker inputMediaOption">
                  <span>本地媒体文件</span>
                  <input
                    type="file"
                    accept="audio/*,video/*"
                    onChange={(event) =>
                      setSelectedFile(event.target.files?.[0] ?? null)
                    }
                    disabled={inputPhase === "live" || inputBusy}
                  />
                </label>
              ) : sourceType === "hls" ? (
                <label className="hlsUrlField inputMediaOption">
                  <span>公开 M3U8 地址</span>
                  <input
                    type="url"
                    value={hlsUrl}
                    onChange={(event) => setHlsUrl(event.target.value)}
                    placeholder="https://example.com/live/index.m3u8"
                    autoComplete="off"
                    spellCheck={false}
                    disabled={inputPhase === "live" || inputBusy}
                  />
                  <small>
                    仅支持无需 Cookie、登录或自定义请求头的公网 HTTP/HTTPS 流。
                  </small>
                </label>
              ) : (
                <div className="sourceHint inputMediaOption">
                  {sourceType === "screen"
                    ? "提示：Chrome 中请选择标签页，并开启“共享标签页音频”。"
                    : "浏览器会在启动时请求麦克风权限。"}
                </div>
              )}
            </div>

            <div className="inputActions">
              <button
                type="button"
                className="primaryAction"
                onClick={() => void startInput()}
                disabled={!canStart}
              >
                {inputBusy ? "准备中…" : "开始生成字幕"}
              </button>
              <button
                type="button"
                className="stopAction"
                onClick={() => void stopInput()}
                disabled={!canStop}
              >
                {inputPhase === "live" ? "停止输入" : "取消等待任务"}
              </button>
              {activeRun !== null ? (
                <div className="runStatus">
                  <span
                    className={`statusPulse statusPulse-${activeRun.status}`}
                  />
                  <div>
                    <small>当前字幕任务</small>
                    <strong>{STATUS_LABELS[captionState.sessionStatus]}</strong>
                  </div>
                  <code>{activeRun.id.slice(0, 8)}</code>
                </div>
              ) : null}
            </div>
          </section>

          <section className="captionStage">
            <div className="captionStageHeader">
              <div>
                <span className="label">Live captions</span>
                <h2>Room 内字幕效果</h2>
              </div>
              <div className="captionStageActions">
                <FloatingCaptions
                  sourceLanguage={displaySourceLanguage}
                  targetLanguage={visibleTargetLanguage}
                  sourceDrafts={drafts}
                  sourceFinals={finals}
                  translationDrafts={translationDrafts}
                  translationFinals={translationFinals}
                  translationStatus={captionState.translationStatus}
                />
                <div className="captionCounters">
                  <span>{finals.length} 条原文</span>
                  {visibleTargetLanguage !== null ? (
                    <span>{translationFinals.length} 条译文</span>
                  ) : null}
                  <span>{formatAudioTime(captionState.currentAudioTimeMs)}</span>
                </div>
              </div>
            </div>

            {captionState.error !== null ? (
              <p className="captionInlineError">{captionState.error}</p>
            ) : null}

            <div
              className={
                visibleTargetLanguage === null
                  ? "captionLanes captionLanes-single"
                  : "captionLanes"
              }
            >
              <section
                ref={sourceCaptionLaneRef}
                className="captionLane captionLane-source"
              >
                <div className="captionLaneHeading">
                  <div>
                    <span>ORIGINAL</span>
                    <strong>
                      {languageLabel(activeRun?.language ?? language)}
                    </strong>
                  </div>
                  <small>{drafts.length} 条实时草稿</small>
                </div>
                <div className="liveDraftArea" aria-live="polite">
                  {sourceDisplayDrafts.length === 0 ? (
                    <p>
                      {activeRun === null
                        ? "启动音频输入后显示源语言字幕。"
                        : "正在等待源语言语音…"}
                    </p>
                  ) : (
                    sourceDisplayDrafts.map((draft) => (
                      <div key={draft.displayId} className="liveDraft">
                        <span>LIVE</span>
                        <p>{draft.text}</p>
                        <small>rev {draft.revision}</small>
                      </div>
                    ))
                  )}
                </div>
                <ol className="finalTranscript" aria-live="polite">
                  {sourceDisplayFinals.map((caption) => (
                    <li key={caption.displayId}>
                      <time>{formatAudioTime(caption.audioStartMs)}</time>
                      <p>{caption.text}</p>
                      <span>
                        {sourceConfidenceBySegment.get(caption.segmentId) === null ||
                        sourceConfidenceBySegment.get(caption.segmentId) === undefined
                          ? "FINAL"
                          : `${Math.round(
                              (sourceConfidenceBySegment.get(caption.segmentId) ?? 0) * 100,
                            )}%`}
                      </span>
                    </li>
                  ))}
                </ol>
              </section>

              {visibleTargetLanguage !== null ? (
                <section
                  ref={translationCaptionLaneRef}
                  className="captionLane captionLane-translation"
                >
                  <div className="captionLaneHeading">
                    <div>
                      <span>TRANSLATION</span>
                      <strong>
                        {languageLabel(visibleTargetLanguage)}
                      </strong>
                    </div>
                    <small
                      className={`translationState translationState-${captionState.translationStatus}`}
                    >
                      {
                        activeRun === null
                          ? "待启动"
                          : TRANSLATION_STATUS_LABELS[
                              captionState.translationStatus
                            ]
                      }
                    </small>
                  </div>
                  {captionState.translationError !== null ? (
                    <p className="translationInlineError">
                      {captionState.translationError}
                      <span>原文字幕仍会继续。</span>
                    </p>
                  ) : null}
                  <div className="liveDraftArea" aria-live="polite">
                    {translationDisplayDrafts.length === 0 ? (
                      <p>
                        {activeRun === null
                          ? "启动后显示目标语言实时译文。"
                          : captionState.translationStatus === "failed"
                            ? "翻译已降级，请检查百炼配置。"
                            : "正在等待实时译文…"}
                      </p>
                    ) : (
                      translationDisplayDrafts.map((draft) => (
                        <div
                          key={draft.displayId}
                          className="liveDraft liveDraft-translation"
                        >
                          <span>LIVE</span>
                          <p>{draft.text}</p>
                          <small>rev {draft.revision}</small>
                        </div>
                      ))
                    )}
                  </div>
                  <ol className="finalTranscript" aria-live="polite">
                    {translationDisplayFinals.map((caption) => (
                      <li key={caption.displayId}>
                        <time>
                          {formatAudioTime(caption.audioStartMs)}
                        </time>
                        <p>{caption.text}</p>
                        <span>FINAL</span>
                      </li>
                    ))}
                  </ol>
                </section>
              ) : null}
            </div>
          </section>
        </section>

        <aside className="studioRight">
          <section className="studioPanel participantPanel">
            <div className="panelHeading">
              <div>
                <span className="label">LiveKit runtime</span>
                <h2>参与者与轨道</h2>
              </div>
              <strong>{participants.length}</strong>
            </div>
            {participants.length === 0 ? (
              <p className="emptyState">进入 Room 后显示在线参与者。</p>
            ) : (
              <ul className="participantList">
                {participants.map((participant) => (
                  <li key={participant.identity}>
                    <div className="participantIdentity">
                      <span
                        className={
                          participant.isLocal
                            ? "avatar avatar-local"
                            : participant.identity.startsWith("agent-")
                              ? "avatar avatar-agent"
                              : "avatar"
                        }
                      >
                        {participant.isLocal
                          ? "ME"
                          : participant.identity.startsWith("agent-")
                            ? "AI"
                            : "GU"}
                      </span>
                      <div>
                        <strong>{participant.identity}</strong>
                        <small>
                          {participant.isLocal
                            ? "当前控制台"
                            : participant.identity.startsWith("agent-")
                              ? "字幕 Worker"
                              : "远端参与者"}
                        </small>
                      </div>
                      {!participant.isLocal &&
                      !participant.identity.startsWith("agent-") ? (
                        <button
                          type="button"
                          className="textDanger"
                          onClick={() =>
                            void removeParticipant(participant.identity)
                          }
                        >
                          移除
                        </button>
                      ) : null}
                    </div>
                    <div className="trackList">
                      {participant.tracks.length === 0 ? (
                        <span>未发布轨道</span>
                      ) : (
                        participant.tracks.map((track) => (
                          <div key={track.sid}>
                            <span>{track.kind === Track.Kind.Audio ? "AUD" : "VID"}</span>
                            <div>
                              <code>{track.name}</code>
                              <small>
                                {track.source}
                                {track.muted ? " · 已静音" : " · 活跃"}
                              </small>
                            </div>
                          </div>
                        ))
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="studioPanel runHistoryPanel">
            <div className="panelHeading">
              <div>
                <span className="label">Caption runs</span>
                <h2>字幕任务记录</h2>
              </div>
              {selectedRoom !== null ? (
                <button
                  type="button"
                  className="iconButton"
                  onClick={() => void refreshRuns(selectedRoom.id)}
                  aria-label="刷新任务"
                >
                  ↻
                </button>
              ) : null}
            </div>
            {runs.length === 0 ? (
              <p className="emptyState">当前 Room 还没有字幕任务。</p>
            ) : (
              <div className="runList">
                {runs.map((run) => (
                  <button
                    type="button"
                    key={run.id}
                    className={
                      activeRun?.id === run.id ? "runItem active" : "runItem"
                    }
                    onClick={() => void inspectRun(run)}
                    disabled={inputPhase === "live"}
                  >
                    <span>
                      <strong>{run.source_name}</strong>
                      <i className={`runState runState-${run.status}`}>
                        {STATUS_LABELS[run.status]}
                      </i>
                    </span>
                    <small>
                      {SOURCE_LABELS[run.source_type]} · {formatDate(run.created_at)}
                    </small>
                  </button>
                ))}
              </div>
            )}
            {activeRun !== null ? (
              <>
                <div className="runExports">
                  <span>导出当前台本</span>
                  <a href={`/sessions/${encodeURIComponent(activeRun.id)}`} target="_blank" rel="noopener noreferrer">
                    后处理
                  </a>
                  {(["json", "srt", "vtt", "markdown"] as const).map(
                    (format) => (
                      <a
                        key={format}
                        href={getSessionExportUrl(activeRun.id, format)}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        {format.toUpperCase()}
                      </a>
                    ),
                  )}
                </div>
                <details className="runtimeDiagnostics">
                  <summary>
                    <span>运行诊断</span>
                    <small>
                      {TERMINAL_STATUSES.has(activeRun.status)
                        ? "终态快照"
                        : "每 2 秒刷新"}
                    </small>
                  </summary>
                  {runtimeError !== null ? (
                    <p className="runtimeDiagnosticError">{runtimeError}</p>
                  ) : runtimeSnapshot === null ? (
                    <p className="emptyState">正在读取运行快照…</p>
                  ) : (
                    <div className="runtimeDiagnosticBody">
                      <div className="runtimeStates">
                        <span>Session <b>{runtimeSnapshot.session_status}</b></span>
                        <span>Source <b>{runtimeSnapshot.source_status}</b></span>
                        <span>Translation <b>{runtimeSnapshot.translation_status}</b></span>
                        <span>Cleanup <b>{runtimeSnapshot.cleanup_status}</b></span>
                      </div>
                      <dl className="runtimeFacts">
                        <div><dt>Room</dt><dd>{runtimeFlag(runtimeSnapshot.room_connected)}</dd></div>
                        <div><dt>发布端</dt><dd>{runtimeFlag(runtimeSnapshot.publisher_connected)}</dd></div>
                        <div><dt>音轨发布</dt><dd>{runtimeFlag(runtimeSnapshot.track_published)}</dd></div>
                        <div><dt>音轨订阅</dt><dd>{runtimeFlag(runtimeSnapshot.track_subscribed)}</dd></div>
                        <div><dt>ASR</dt><dd>{runtimeFlag(runtimeSnapshot.asr_connected)}</dd></div>
                        <div><dt>翻译</dt><dd>{runtimeFlag(runtimeSnapshot.translation_connected)}</dd></div>
                        <div><dt>FFmpeg</dt><dd>{runtimeFlag(runtimeSnapshot.ffmpeg_running)}</dd></div>
                        <div><dt>音频帧</dt><dd>{runtimeSnapshot.audio_frames ?? "不可用"}</dd></div>
                        <div><dt>音频字节</dt><dd>{formatBytes(runtimeSnapshot.audio_bytes)}</dd></div>
                        <div><dt>ASR 队列</dt><dd>{runtimeSnapshot.audio_queue_current === null ? "不可用" : `${runtimeSnapshot.audio_queue_current} / ${runtimeSnapshot.audio_queue_max ?? "?"}`}</dd></div>
                        <div><dt>源字幕 Final</dt><dd>{runtimeSnapshot.source_final_count}</dd></div>
                        <div><dt>翻译 Final</dt><dd>{runtimeSnapshot.translation_final_count}</dd></div>
                        <div><dt>开始时间</dt><dd>{runtimeSnapshot.started_at === null ? "不可用" : formatDate(runtimeSnapshot.started_at)}</dd></div>
                        <div><dt>最后事件</dt><dd>{runtimeSnapshot.last_event_at === null ? "不可用" : formatDate(runtimeSnapshot.last_event_at)}</dd></div>
                      </dl>
                      {runtimeSnapshot.failure_code !== null || runtimeSnapshot.stop_reason !== null ? (
                        <p className="runtimeTerminalReason">
                          {runtimeSnapshot.failure_code ?? runtimeSnapshot.stop_reason}
                        </p>
                      ) : null}
                      {runtimeSnapshot.unavailable.length > 0 ? (
                        <p className="runtimeUnavailable">
                          当前不可用：{runtimeSnapshot.unavailable.join("、")}
                        </p>
                      ) : null}
                    </div>
                  )}
                </details>
              </>
            ) : null}
          </section>
        </aside>
      </div>
    </main>
  );
}
