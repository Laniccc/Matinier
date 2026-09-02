import { describe, expect, it } from "vitest";

import {
  DEFAULT_FLOATING_CAPTION_PREFERENCES,
  FLOATING_CAPTION_STORAGE_KEY,
  loadFloatingCaptionPreferences,
  resolveFloatingCaptionMode,
  selectFloatingCaptionMode,
  updateFloatingCaptionPreferences,
} from "./floating-caption-state";

function storageWith(value: string | null) {
  return {
    getItem: (key: string) =>
      key === FLOATING_CAPTION_STORAGE_KEY ? value : null,
    setItem: () => undefined,
  };
}

describe("floating caption preferences", () => {
  it("defaults to translated captions", () => {
    expect(loadFloatingCaptionPreferences(storageWith(null))).toEqual(
      DEFAULT_FLOATING_CAPTION_PREFERENCES,
    );
  });

  it("falls back to source when the Session has no target language", () => {
    const resolved = resolveFloatingCaptionMode(
      DEFAULT_FLOATING_CAPTION_PREFERENCES,
      { targetLanguage: null, translationStatus: "disabled" },
    );

    expect(resolved.effectiveMode).toBe("source");
    expect(resolved.notice).toBe("当前任务未启用翻译");
  });

  it("falls back to source when translation fails", () => {
    const resolved = resolveFloatingCaptionMode(
      DEFAULT_FLOATING_CAPTION_PREFERENCES,
      { targetLanguage: "en-US", translationStatus: "failed" },
    );

    expect(resolved.effectiveMode).toBe("source");
    expect(resolved.notice).toBe("翻译中断，已切换原文");
  });

  it("keeps a manually selected source mode after translation recovers", () => {
    const selected = selectFloatingCaptionMode(
      DEFAULT_FLOATING_CAPTION_PREFERENCES,
      "source",
    );
    const resolved = resolveFloatingCaptionMode(selected, {
      targetLanguage: "en-US",
      translationStatus: "running",
    });

    expect(selected.modeWasSelectedByUser).toBe(true);
    expect(resolved.effectiveMode).toBe("source");
    expect(resolved.notice).toBeNull();
  });

  it("returns defaults for malformed persisted JSON", () => {
    expect(loadFloatingCaptionPreferences(storageWith("{broken"))).toEqual(
      DEFAULT_FLOATING_CAPTION_PREFERENCES,
    );
  });

  it("clamps persisted and updated display values", () => {
    const loaded = loadFloatingCaptionPreferences(storageWith(JSON.stringify({
      version: 1,
      mode: "translation",
      modeWasSelectedByUser: false,
      fontSizePx: 500,
      backgroundOpacity: -2,
    })));
    const updated = updateFloatingCaptionPreferences(loaded, {
      fontSizePx: 2,
      backgroundOpacity: 4,
    });

    expect(loaded.fontSizePx).toBe(56);
    expect(loaded.backgroundOpacity).toBe(0.2);
    expect(updated.fontSizePx).toBe(20);
    expect(updated.backgroundOpacity).toBe(0.95);
  });
});
