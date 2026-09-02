import type { TranslationStatus } from "@/types/captions";

export type FloatingCaptionMode = "translation" | "source" | "bilingual";

export type FloatingCaptionPreferences = {
  version: 1;
  mode: FloatingCaptionMode;
  modeWasSelectedByUser: boolean;
  fontSizePx: number;
  backgroundOpacity: number;
};

export type FloatingCaptionStorage = {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
};

export const FLOATING_CAPTION_STORAGE_KEY =
  "matinier.floating-captions.preferences.v1";

export const DEFAULT_FLOATING_CAPTION_PREFERENCES: FloatingCaptionPreferences = {
  version: 1,
  mode: "translation",
  modeWasSelectedByUser: false,
  fontSizePx: 32,
  backgroundOpacity: 0.68,
};

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value));
}

function isMode(value: unknown): value is FloatingCaptionMode {
  return value === "translation" || value === "source" || value === "bilingual";
}

function normalizedPreferences(value: unknown): FloatingCaptionPreferences {
  if (typeof value !== "object" || value === null) {
    return { ...DEFAULT_FLOATING_CAPTION_PREFERENCES };
  }
  const candidate = value as Record<string, unknown>;
  if (candidate.version !== 1 || !isMode(candidate.mode)) {
    return { ...DEFAULT_FLOATING_CAPTION_PREFERENCES };
  }
  return {
    version: 1,
    mode: candidate.mode,
    modeWasSelectedByUser: candidate.modeWasSelectedByUser === true,
    fontSizePx: clamp(
      typeof candidate.fontSizePx === "number"
        ? candidate.fontSizePx
        : DEFAULT_FLOATING_CAPTION_PREFERENCES.fontSizePx,
      20,
      56,
    ),
    backgroundOpacity: clamp(
      typeof candidate.backgroundOpacity === "number"
        ? candidate.backgroundOpacity
        : DEFAULT_FLOATING_CAPTION_PREFERENCES.backgroundOpacity,
      0.2,
      0.95,
    ),
  };
}

export function loadFloatingCaptionPreferences(
  storage: Pick<FloatingCaptionStorage, "getItem">,
): FloatingCaptionPreferences {
  try {
    const stored = storage.getItem(FLOATING_CAPTION_STORAGE_KEY);
    return stored === null
      ? { ...DEFAULT_FLOATING_CAPTION_PREFERENCES }
      : normalizedPreferences(JSON.parse(stored));
  } catch {
    return { ...DEFAULT_FLOATING_CAPTION_PREFERENCES };
  }
}

export function saveFloatingCaptionPreferences(
  storage: Pick<FloatingCaptionStorage, "setItem">,
  preferences: FloatingCaptionPreferences,
): void {
  try {
    storage.setItem(
      FLOATING_CAPTION_STORAGE_KEY,
      JSON.stringify(normalizedPreferences(preferences)),
    );
  } catch {
    // Storage can be unavailable in private or restricted browser contexts.
  }
}

export function updateFloatingCaptionPreferences(
  current: FloatingCaptionPreferences,
  updates: Partial<Pick<
    FloatingCaptionPreferences,
    "fontSizePx" | "backgroundOpacity"
  >>,
): FloatingCaptionPreferences {
  return normalizedPreferences({ ...current, ...updates });
}

export function selectFloatingCaptionMode(
  current: FloatingCaptionPreferences,
  mode: FloatingCaptionMode,
): FloatingCaptionPreferences {
  return {
    ...current,
    mode,
    modeWasSelectedByUser: true,
  };
}

export function resolveFloatingCaptionMode(
  preferences: FloatingCaptionPreferences,
  availability: {
    targetLanguage: string | null;
    translationStatus: TranslationStatus;
  },
): { effectiveMode: FloatingCaptionMode; notice: string | null } {
  if (preferences.mode === "source") {
    return { effectiveMode: "source", notice: null };
  }
  if (availability.targetLanguage === null) {
    return {
      effectiveMode: "source",
      notice: "当前任务未启用翻译",
    };
  }
  if (availability.translationStatus === "failed") {
    return {
      effectiveMode: "source",
      notice: "翻译中断，已切换原文",
    };
  }
  return { effectiveMode: preferences.mode, notice: null };
}
