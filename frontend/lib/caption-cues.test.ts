import { describe, expect, it } from "vitest";

import {
  composeCaptionCues,
  type CaptionCueInput,
  type CaptionLayoutProfile,
} from "./caption-cues";

const measureText = (text: string) =>
  Array.from(text).reduce(
    (width, char) => width + (/\p{Script=Han}/u.test(char) ? 20 : 10),
    0,
  );

const profile: CaptionLayoutProfile = {
  maxLineWidthPx: 120,
  maxLines: 2,
  minFillRatio: 0.3,
  maxReadingUnitsPerSecond: 18,
  measureText,
};

function input(text: string): CaptionCueInput {
  return {
    segmentId: "segment-1",
    revision: 1,
    status: "final",
    text,
    language: "zh-CN",
    audioStartMs: 0,
    audioEndMs: 4_000,
  };
}

describe("composeCaptionCues", () => {
  it("prefers a strong sentence boundary when the combined text overflows", () => {
    const cues = composeCaptionCues(
      [input("第一句话。第二句话。")],
      { ...profile, maxLines: 1 },
    );

    expect(cues.map((cue) => cue.text)).toEqual([
      "第一句话。",
      "第二句话。",
    ]);
  });

  it("never emits a line wider than the selected profile", () => {
    const cues = composeCaptionCues(
      [input("这是一句没有停顿而且非常长的测试文字")],
      profile,
    );

    expect(
      cues.flatMap((cue) => cue.lines)
        .every((line) => measureText(line) <= 120),
    ).toBe(true);
  });

  it("prefers a comma over a hard split for an oversized sentence", () => {
    const cues = composeCaptionCues(
      [input("前半句提供背景信息，后半句给出最终结论。")],
      { ...profile, maxLineWidthPx: 100 },
    );

    expect(cues[0]?.text.endsWith("，")).toBe(true);
  });

  it("does not cut an English word", () => {
    const english = {
      ...input("Adaptive subtitles preserve complete words."),
      language: "en-US",
    };
    const cues = composeCaptionCues(
      [english],
      { ...profile, maxLineWidthPx: 90 },
    );

    expect(cues.flatMap((cue) => cue.lines).join(" "))
      .not.toContain("subti tles");
    expect(cues.map((cue) => cue.text).join(" ")).toContain("subtitles");
  });

  it("keeps an emoji grapheme intact", () => {
    const cues = composeCaptionCues(
      [input("现在开始测试👨‍👩‍👧‍👦然后继续说明")],
      { ...profile, maxLineWidthPx: 80 },
    );

    expect(cues.map((cue) => cue.text).join(""))
      .toContain("👨‍👩‍👧‍👦");
  });

  it("merges a short fragment with an adjacent sentence when both fit", () => {
    const cues = composeCaptionCues(
      [input("好。下面开始介绍主要内容。")],
      { ...profile, maxLineWidthPx: 180 },
    );

    expect(cues[0]?.text).toBe("好。下面开始介绍主要内容。");
    expect(cues).toHaveLength(1);
  });

  it.each(["；", ";", "：", ":", "，", ",", "、"])(
    "uses %s as a clause boundary before a hard split",
    (punctuation) => {
      const cues = composeCaptionCues(
        [input(`前半部分稍微长些${punctuation}后半部分继续解释。`)],
        { ...profile, maxLineWidthPx: 90 },
      );

      expect(cues.some((cue) => cue.text.endsWith(punctuation))).toBe(true);
    },
  );

  it("normalizes repeated spaces and embedded newlines", () => {
    const cues = composeCaptionCues(
      [{ ...input("First   line\nSecond line."), language: "en-US" }],
      { ...profile, maxLineWidthPx: 200 },
    );

    expect(cues.map((cue) => cue.text).join(" ")).toBe(
      "First line Second line.",
    );
  });

  it("keeps a fitting number token intact", () => {
    const cues = composeCaptionCues(
      [{ ...input("Date 2026-08-26 confirmed."), language: "en-US" }],
      { ...profile, maxLineWidthPx: 110 },
    );

    expect(cues.map((cue) => cue.text).join(" ")).toContain("2026-08-26");
  });

  it("returns no cues for empty text", () => {
    expect(composeCaptionCues([input("   \n  ")], profile)).toEqual([]);
  });

  it("rejects an invalid layout profile", () => {
    expect(() => composeCaptionCues(
      [input("测试")],
      { ...profile, maxLineWidthPx: 0 },
    )).toThrow("maxLineWidthPx");
  });

  it("allocates a contiguous source time range across split cues", () => {
    const cues = composeCaptionCues(
      [{
        ...input("第一部分内容较长，需要拆分。第二部分也需要足够阅读时间。"),
        audioEndMs: 6_000,
      }],
      { ...profile, maxLineWidthPx: 100 },
    );

    expect(cues.length).toBeGreaterThan(1);
    expect(cues[0]?.audioStartMs).toBe(0);
    expect(cues.at(-1)?.audioEndMs).toBe(6_000);
    expect(cues.every((cue, index) =>
      index === 0 || cue.audioStartMs === cues[index - 1]?.audioEndMs
    )).toBe(true);
  });

  it("splits a readable layout again when its source duration is overloaded", () => {
    const cues = composeCaptionCues(
      [{
        ...input("第一段内容，第二段内容，第三段内容。"),
        audioEndMs: 1_000,
      }],
      {
        ...profile,
        maxLineWidthPx: 500,
        maxReadingUnitsPerSecond: 4,
      },
    );

    expect(cues.length).toBeGreaterThan(1);
    expect(cues.some((cue) => cue.text.endsWith("，"))).toBe(true);
  });

  it("does not invent cue timestamps when the source range is unknown", () => {
    const cues = composeCaptionCues(
      [{ ...input("第一句。第二句。"), audioStartMs: null, audioEndMs: null }],
      { ...profile, maxLines: 1 },
    );

    expect(cues.every((cue) =>
      cue.audioStartMs === null && cue.audioEndMs === null
    )).toBe(true);
  });
});
