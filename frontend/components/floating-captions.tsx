"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { createPortal } from "react-dom";

import { composeCaptionCues } from "@/lib/caption-cues";
import type { CaptionLayoutProfile } from "@/lib/caption-cues";
import { createCanvasTextMeasurer } from "@/lib/caption-measurement";
import {
  DEFAULT_FLOATING_CAPTION_PREFERENCES,
  loadFloatingCaptionPreferences,
  resolveFloatingCaptionMode,
  saveFloatingCaptionPreferences,
  selectFloatingCaptionMode,
  updateFloatingCaptionPreferences,
} from "@/lib/floating-caption-state";
import type {
  FloatingCaptionMode,
  FloatingCaptionPreferences,
} from "@/lib/floating-caption-state";
import {
  FloatingCaptionWindowError,
  requestFloatingCaptionWindow,
} from "@/lib/floating-caption-window";
import {
  advanceLiveCaptionView,
  createLiveCaptionViewState,
} from "@/lib/live-caption-view";
import type {
  LiveCaptionCandidate,
  LiveCaptionViewState,
} from "@/lib/live-caption-view";
import type {
  CaptionPayload,
  TranslationPayload,
  TranslationStatus,
} from "@/types/captions";

type FloatingCaptionsProps = {
  sourceLanguage: string;
  targetLanguage: string | null;
  sourceDrafts: readonly CaptionPayload[];
  sourceFinals: readonly CaptionPayload[];
  translationDrafts: readonly TranslationPayload[];
  translationFinals: readonly TranslationPayload[];
  translationStatus: TranslationStatus;
};

type CaptionLike = {
  segment_id: string;
  revision: number;
  status: "draft" | "final";
  text: string;
  audio_start_ms: number | null;
  audio_end_ms: number | null;
  received_at_ms: number;
};

const MODE_LABELS: Array<{ mode: FloatingCaptionMode; label: string }> = [
  { mode: "translation", label: "译文" },
  { mode: "source", label: "原文" },
  { mode: "bilingual", label: "双语" },
];

function timeKey(value: number | null): number {
  return value ?? Number.NEGATIVE_INFINITY;
}

function newestCaption<T extends CaptionLike>(
  drafts: readonly T[],
  finals: readonly T[],
): T | null {
  const values = drafts.length > 0 ? drafts : finals;
  return values.reduce<T | null>((latest, candidate) => {
    if (latest === null) {
      return candidate;
    }
    const order =
      timeKey(candidate.audio_start_ms) - timeKey(latest.audio_start_ms) ||
      candidate.received_at_ms - latest.received_at_ms ||
      candidate.revision - latest.revision ||
      candidate.segment_id.localeCompare(latest.segment_id);
    return order >= 0 ? candidate : latest;
  }, null);
}

function toLiveCandidate(
  lane: "source" | "translation",
  caption: CaptionLike | null,
): LiveCaptionCandidate | null {
  if (caption === null || !caption.text.trim()) {
    return null;
  }
  return {
    key: `${lane}:${caption.segment_id}:${caption.revision}:${caption.status}`,
    segmentId: caption.segment_id,
    revision: caption.revision,
    status: caption.status,
    text: caption.text,
  };
}

function useStableLiveCandidate(
  candidate: LiveCaptionCandidate | null,
): LiveCaptionViewState {
  const [state, setState] = useState<LiveCaptionViewState>(() =>
    createLiveCaptionViewState(),
  );
  const stateRef = useRef(state);

  useEffect(() => {
    const nowMs = performance.now();
    const result = advanceLiveCaptionView(stateRef.current, {
      candidate,
      nowMs,
    });
    stateRef.current = result.state;
    setState(result.state);

    if (result.nextUpdateAtMs === null) {
      return;
    }
    const timer = window.setTimeout(() => {
      const promoted = advanceLiveCaptionView(stateRef.current, {
        candidate: null,
        nowMs: result.nextUpdateAtMs ?? performance.now(),
      });
      stateRef.current = promoted.state;
      setState(promoted.state);
    }, Math.max(0, result.nextUpdateAtMs - performance.now()));
    return () => window.clearTimeout(timer);
  }, [candidate?.key, candidate?.status, candidate?.text]);

  return state;
}

function clonePageStyles(target: Document): void {
  target.head.querySelectorAll("[data-matinier-cloned-style]").forEach(
    (node) => node.remove(),
  );
  document.querySelectorAll('link[rel="stylesheet"], style').forEach((node) => {
    const clone = node.cloneNode(true) as Element;
    clone.setAttribute("data-matinier-cloned-style", "true");
    target.head.append(clone);
  });
}

function availableBrowserStorage(): Storage | null {
  try {
    return window.localStorage ?? null;
  } catch {
    return null;
  }
}

function readingRate(language: string): number {
  const base = language.toLowerCase().split("-")[0];
  return base === "zh" || base === "ja" || base === "ko" ? 18 : 4.5;
}

function composedLines(
  candidate: LiveCaptionCandidate | null,
  language: string,
  profile: CaptionLayoutProfile,
): string[] {
  if (candidate === null) {
    return [];
  }
  const cues = composeCaptionCues([{
    segmentId: candidate.segmentId,
    revision: candidate.revision,
    status: candidate.status,
    text: candidate.text,
    language,
    audioStartMs: null,
    audioEndMs: null,
  }], profile);
  return cues.at(-1)?.lines ?? [];
}

export function FloatingCaptions({
  sourceLanguage,
  targetLanguage,
  sourceDrafts,
  sourceFinals,
  translationDrafts,
  translationFinals,
  translationStatus,
}: FloatingCaptionsProps) {
  const [preferences, setPreferences] =
    useState<FloatingCaptionPreferences>(() => ({
      ...DEFAULT_FLOATING_CAPTION_PREFERENCES,
    }));
  const [preferencesLoaded, setPreferencesLoaded] = useState(false);
  const [supported, setSupported] = useState<boolean | null>(null);
  const [pipWindow, setPipWindow] = useState<Window | null>(null);
  const [portalRoot, setPortalRoot] = useState<HTMLElement | null>(null);
  const [viewportWidth, setViewportWidth] = useState(720);
  const [message, setMessage] = useState<string | null>(null);
  const pipWindowRef = useRef<Window | null>(null);

  useEffect(() => {
    setSupported(window.documentPictureInPicture !== undefined);
  }, []);

  useEffect(() => {
    const storage = availableBrowserStorage();
    if (storage !== null) {
      setPreferences(loadFloatingCaptionPreferences(storage));
    }
    setPreferencesLoaded(true);
  }, []);

  useEffect(() => {
    const storage = availableBrowserStorage();
    if (preferencesLoaded && storage !== null) {
      saveFloatingCaptionPreferences(storage, preferences);
    }
  }, [preferences, preferencesLoaded]);

  const sourceCaption = useMemo(
    () => newestCaption(sourceDrafts, sourceFinals),
    [sourceDrafts, sourceFinals],
  );
  const translationCaption = useMemo(
    () => newestCaption(translationDrafts, translationFinals),
    [translationDrafts, translationFinals],
  );
  const sourceCandidate = useMemo(
    () => toLiveCandidate("source", sourceCaption),
    [sourceCaption],
  );
  const translationCandidate = useMemo(
    () => toLiveCandidate("translation", translationCaption),
    [translationCaption],
  );
  const sourceView = useStableLiveCandidate(sourceCandidate);
  const translationView = useStableLiveCandidate(translationCandidate);
  const resolvedMode = resolveFloatingCaptionMode(preferences, {
    targetLanguage,
    translationStatus,
  });

  const textMeasurer = useMemo(
    () => createCanvasTextMeasurer(
      `${preferences.fontSizePx}px Inter, ui-sans-serif, system-ui, "Segoe UI", sans-serif`,
    ),
    [preferences.fontSizePx],
  );
  const profileFor = useCallback((language: string): CaptionLayoutProfile => ({
    maxLineWidthPx: Math.max(120, viewportWidth - 48),
    maxLines: 2,
    minFillRatio: 0.3,
    maxReadingUnitsPerSecond: readingRate(language),
    measureText: textMeasurer,
  }), [textMeasurer, viewportWidth]);
  const sourceLines = useMemo(
    () => composedLines(
      sourceView.visible,
      sourceLanguage,
      profileFor(sourceLanguage),
    ),
    [profileFor, sourceLanguage, sourceView.visible],
  );
  const translationLanguage = targetLanguage ?? "en-US";
  const translationLines = useMemo(
    () => composedLines(
      translationView.visible,
      translationLanguage,
      profileFor(translationLanguage),
    ),
    [profileFor, translationLanguage, translationView.visible],
  );

  const clearWindowState = useCallback(() => {
    pipWindowRef.current = null;
    setPipWindow(null);
    setPortalRoot(null);
  }, []);

  const closeFloatingWindow = useCallback(() => {
    const current = pipWindowRef.current;
    clearWindowState();
    if (current !== null && !current.closed) {
      current.close();
    }
  }, [clearWindowState]);

  useEffect(() => {
    if (pipWindow === null) {
      return;
    }
    const handlePageHide = () => clearWindowState();
    const handleResize = () => {
      if (!pipWindow.closed) {
        setViewportWidth(Math.max(1, pipWindow.innerWidth));
      }
    };
    pipWindow.addEventListener("pagehide", handlePageHide);
    pipWindow.addEventListener("resize", handleResize);
    handleResize();
    return () => {
      pipWindow.removeEventListener("pagehide", handlePageHide);
      pipWindow.removeEventListener("resize", handleResize);
    };
  }, [clearWindowState, pipWindow]);

  useEffect(() => () => {
    const current = pipWindowRef.current;
    if (current !== null && !current.closed) {
      current.close();
    }
  }, []);

  const openFloatingWindow = useCallback(async () => {
    setMessage(null);
    try {
      const opened = await requestFloatingCaptionWindow(window);
      const targetWindow = opened.window;
      const targetDocument = targetWindow.document;
      targetDocument.title = "Matinier 悬浮字幕";
      targetDocument.documentElement.classList.add("floatingCaptionDocument");
      targetDocument.body.classList.add("floatingCaptionDocument");
      clonePageStyles(targetDocument);

      let root = targetDocument.getElementById("matinier-floating-caption-root");
      if (root === null) {
        root = targetDocument.createElement("div");
        root.id = "matinier-floating-caption-root";
        root.className = "floatingCaptionRoot";
        targetDocument.body.replaceChildren(root);
      }
      pipWindowRef.current = targetWindow;
      setPipWindow(targetWindow);
      setPortalRoot(root);
      setViewportWidth(Math.max(1, targetWindow.innerWidth));
    } catch (error) {
      clearWindowState();
      setMessage(
        error instanceof FloatingCaptionWindowError
          ? error.message
          : "悬浮字幕窗口打开失败，请稍后重试。",
      );
    }
  }, [clearWindowState]);

  const changeMode = (mode: FloatingCaptionMode) => {
    setPreferences((current) => selectFloatingCaptionMode(current, mode));
  };
  const updateDisplay = (
    updates: Partial<Pick<
      FloatingCaptionPreferences,
      "fontSizePx" | "backgroundOpacity"
    >>,
  ) => {
    setPreferences((current) =>
      updateFloatingCaptionPreferences(current, updates),
    );
  };

  const transition = resolvedMode.effectiveMode === "source"
    ? sourceView.transition
    : translationView.transition;
  const portalStyle = {
    "--floating-caption-font-size": `${preferences.fontSizePx}px`,
    "--floating-caption-opacity": preferences.backgroundOpacity,
  } as CSSProperties;

  const floatingContent = portalRoot === null ? null : createPortal(
    <div className="floatingCaptionSurface" style={portalStyle}>
      <div className="floatingCaptionToolbar" aria-label="悬浮字幕设置">
        <div className="floatingCaptionModes" aria-label="字幕语言">
          {MODE_LABELS.map(({ mode, label }) => (
            <button
              key={mode}
              type="button"
              className={preferences.mode === mode ? "is-active" : undefined}
              aria-pressed={preferences.mode === mode}
              disabled={targetLanguage === null && mode !== "source"}
              onClick={() => changeMode(mode)}
            >
              {label}
            </button>
          ))}
        </div>
        <button
          type="button"
          aria-label="减小字号"
          onClick={() => updateDisplay({
            fontSizePx: preferences.fontSizePx - 2,
          })}
        >
          A−
        </button>
        <button
          type="button"
          aria-label="增大字号"
          onClick={() => updateDisplay({
            fontSizePx: preferences.fontSizePx + 2,
          })}
        >
          A+
        </button>
        <label className="floatingCaptionOpacity">
          <span>背景</span>
          <input
            type="range"
            min="0.2"
            max="0.95"
            step="0.05"
            value={preferences.backgroundOpacity}
            onChange={(event) => updateDisplay({
              backgroundOpacity: Number(event.target.value),
            })}
          />
        </label>
        <button
          type="button"
          aria-label="关闭悬浮字幕"
          onClick={closeFloatingWindow}
        >
          关闭
        </button>
      </div>

      <div
        className="floatingCaptionContent"
        aria-live="polite"
        aria-atomic="true"
      >
        {resolvedMode.notice !== null ? (
          <small className="floatingCaptionNotice">{resolvedMode.notice}</small>
        ) : null}

        {resolvedMode.effectiveMode === "translation" ? (
          <CaptionLines
            lines={translationLines}
            emptyText="正在等待译文…"
            transition={transition}
          />
        ) : resolvedMode.effectiveMode === "source" ? (
          <CaptionLines
            lines={sourceLines}
            emptyText="正在等待原文字幕…"
            transition={transition}
          />
        ) : (
          <div className="floatingCaptionBilingual">
            <CaptionLines
              lines={sourceLines}
              emptyText="正在等待原文字幕…"
              secondary
              transition={sourceView.transition}
            />
            <CaptionLines
              lines={translationLines}
              emptyText="正在等待译文…"
              transition={translationView.transition}
            />
          </div>
        )}
      </div>
    </div>,
    portalRoot,
  );

  return (
    <div className="floatingCaptionControl">
      <label className="floatingCaptionSwitch">
        <input
          type="checkbox"
          role="switch"
          checked={pipWindow !== null && !pipWindow.closed}
          disabled={supported !== true}
          onChange={(event) => {
            if (event.target.checked) {
              void openFloatingWindow();
            } else {
              closeFloatingWindow();
            }
          }}
        />
        <span>悬浮字幕</span>
      </label>
      {supported === false ? (
        <small>请使用桌面版 Chrome 或 Edge</small>
      ) : message !== null ? (
        <small role="alert">{message}</small>
      ) : null}
      {floatingContent}
    </div>
  );
}

function CaptionLines({
  lines,
  emptyText,
  secondary = false,
  transition,
}: {
  lines: readonly string[];
  emptyText: string;
  secondary?: boolean;
  transition: LiveCaptionViewState["transition"];
}) {
  return (
    <p
      className={[
        "floatingCaptionText",
        secondary ? "floatingCaptionText-secondary" : "",
        transition === "final-replace"
          ? "floatingCaptionText-finalReplace"
          : "",
      ].filter(Boolean).join(" ")}
    >
      {(lines.length > 0 ? lines : [emptyText]).map((line, index) => (
        <span key={`${index}:${line}`}>{line}</span>
      ))}
    </p>
  );
}
