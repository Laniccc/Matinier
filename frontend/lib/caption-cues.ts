export type CueStatus = "draft" | "final";

export type CaptionCueInput = {
  segmentId: string;
  revision: number;
  status: CueStatus;
  text: string;
  language: string;
  audioStartMs: number | null;
  audioEndMs: number | null;
};

export type TextWidthMeasurer = (text: string) => number;

export type CaptionLayoutProfile = {
  maxLineWidthPx: number;
  maxLines: 1 | 2;
  minFillRatio: number;
  maxReadingUnitsPerSecond: number;
  measureText: TextWidthMeasurer;
};

export type CaptionDisplayCue = CaptionCueInput & {
  displayId: string;
  text: string;
  lines: string[];
};

const STRONG_BOUNDARY = /[。！？!?\n]/u;
const CLAUSE_BOUNDARY = /[；;：:，,、]/u;

function validateProfile(profile: CaptionLayoutProfile): void {
  if (!Number.isFinite(profile.maxLineWidthPx) || profile.maxLineWidthPx <= 0) {
    throw new Error("maxLineWidthPx must be a positive finite number");
  }
  if (profile.maxLines !== 1 && profile.maxLines !== 2) {
    throw new Error("maxLines must be 1 or 2");
  }
  if (
    !Number.isFinite(profile.minFillRatio) ||
    profile.minFillRatio < 0 ||
    profile.minFillRatio > 1
  ) {
    throw new Error("minFillRatio must be between 0 and 1");
  }
  if (
    !Number.isFinite(profile.maxReadingUnitsPerSecond) ||
    profile.maxReadingUnitsPerSecond <= 0
  ) {
    throw new Error("maxReadingUnitsPerSecond must be positive");
  }
}

function normalizeText(text: string): string {
  return text
    .replace(/[\t\f\v ]+/gu, " ")
    .replace(/ *\n+ */gu, "\n")
    .trim();
}

function graphemes(text: string, language: string): string[] {
  if (typeof Intl.Segmenter === "function") {
    const segmenter = new Intl.Segmenter(language, { granularity: "grapheme" });
    return Array.from(segmenter.segment(text), ({ segment }) => segment);
  }
  return Array.from(text);
}

function measure(text: string, profile: CaptionLayoutProfile): number {
  const width = profile.measureText(text);
  if (!Number.isFinite(width) || width < 0) {
    throw new Error("measureText must return a non-negative finite number");
  }
  return width;
}

function sentenceSpans(text: string): string[] {
  return (text.match(/[^。！？!?\n]+[。！？!?]?/gu) ?? [text])
    .map((value) => value.trim())
    .filter(Boolean);
}

function isSpaceSeparatedLanguage(language: string): boolean {
  const base = language.trim().toLowerCase().split("-")[0];
  return base !== "zh" && base !== "ja" && base !== "ko";
}

function joinText(left: string, right: string, language: string): string {
  if (!left) {
    return right;
  }
  if (!right) {
    return left;
  }
  return isSpaceSeparatedLanguage(language)
    ? `${left.trimEnd()} ${right.trimStart()}`
    : `${left.trimEnd()}${right.trimStart()}`;
}

function fittingGraphemeCount(
  parts: readonly string[],
  profile: CaptionLayoutProfile,
): number {
  let count = 0;
  let candidate = "";
  for (const part of parts) {
    const next = `${candidate}${part}`;
    if (measure(next, profile) > profile.maxLineWidthPx) {
      break;
    }
    candidate = next;
    count += 1;
  }
  return count;
}

function preferredBreakIndex(
  parts: readonly string[],
  fittingCount: number,
  profile: CaptionLayoutProfile,
  allowShortStrongMerge: boolean,
): number {
  let strong = 0;
  let clause = 0;
  let word = 0;

  for (let index = 1; index <= fittingCount; index += 1) {
    const part = parts[index - 1] ?? "";
    if (STRONG_BOUNDARY.test(part)) {
      const prefix = parts.slice(0, index).join("").trim();
      const isShort =
        measure(prefix, profile) <
        profile.maxLineWidthPx * profile.minFillRatio;
      if (!allowShortStrongMerge || !isShort) {
        strong = index;
      }
    } else if (CLAUSE_BOUNDARY.test(part)) {
      clause = index;
    } else if (/\s/u.test(part)) {
      word = index;
    }
  }
  return strong || clause || word || fittingCount;
}

function trimLeadingBoundaryWhitespace(parts: string[]): string[] {
  while (parts.length > 0 && /^\s+$/u.test(parts[0] ?? "")) {
    parts.shift();
  }
  return parts;
}

function wrapText(
  text: string,
  language: string,
  profile: CaptionLayoutProfile,
  allowShortStrongMerge = false,
): string[] {
  let parts = graphemes(text, language);
  const lines: string[] = [];

  while (parts.length > 0) {
    let fittingCount = fittingGraphemeCount(parts, profile);
    if (fittingCount === 0) {
      // A single grapheme can be wider than the configured line. Keeping the
      // grapheme intact is safer than slicing an emoji or combining mark.
      fittingCount = 1;
    }
    const breakIndex = fittingCount === parts.length
      ? fittingCount
      : preferredBreakIndex(
          parts,
          fittingCount,
          profile,
          allowShortStrongMerge,
        );
    const line = parts.slice(0, breakIndex).join("").trim();
    if (line) {
      lines.push(line);
    }
    parts = trimLeadingBoundaryWhitespace(parts.slice(breakIndex));
  }
  return lines;
}

type RawCue = {
  text: string;
  lines: string[];
};

function rawCuesForSpan(
  span: string,
  language: string,
  profile: CaptionLayoutProfile,
): RawCue[] {
  const lines = wrapText(span, language, profile);
  const cues: RawCue[] = [];
  for (let index = 0; index < lines.length; index += profile.maxLines) {
    const cueLines = lines.slice(index, index + profile.maxLines);
    cues.push({
      text: cueLines.reduce(
        (current, line) => joinText(current, line, language),
        "",
      ),
      lines: cueLines,
    });
  }
  return cues;
}

function tryMergedCue(
  left: RawCue,
  right: RawCue,
  language: string,
  profile: CaptionLayoutProfile,
): RawCue | null {
  const text = joinText(left.text, right.text, language);
  const lines = wrapText(text, language, profile, true);
  if (lines.length > profile.maxLines) {
    return null;
  }
  return { text, lines };
}

function mergeShortCues(
  values: readonly RawCue[],
  language: string,
  profile: CaptionLayoutProfile,
): RawCue[] {
  const cues = values.map((value) => ({ ...value, lines: [...value.lines] }));
  const shortThreshold =
    profile.maxLineWidthPx * profile.maxLines * profile.minFillRatio;

  for (let index = 0; index < cues.length;) {
    const cue = cues[index];
    if (!cue || measure(cue.text, profile) >= shortThreshold) {
      index += 1;
      continue;
    }

    const next = cues[index + 1];
    if (next) {
      const merged = tryMergedCue(cue, next, language, profile);
      if (merged) {
        cues.splice(index, 2, merged);
        continue;
      }
    }

    const previous = cues[index - 1];
    if (previous) {
      const merged = tryMergedCue(previous, cue, language, profile);
      if (merged) {
        cues.splice(index - 1, 2, merged);
        index = Math.max(0, index - 1);
        continue;
      }
    }
    index += 1;
  }
  return cues;
}

function readingUnits(text: string, language: string): number {
  let units = 0;
  let inLatinWord = false;
  for (const part of graphemes(text, language)) {
    if (/\p{Script=Han}|\p{Script=Hiragana}|\p{Script=Katakana}|\p{Script=Hangul}/u.test(part)) {
      units += 1;
      inLatinWord = false;
    } else if (/[\p{L}\p{N}]/u.test(part)) {
      if (!inLatinWord) {
        units += 1;
      }
      inLatinWord = true;
    } else {
      if (/\p{Extended_Pictographic}/u.test(part)) {
        units += 1;
      }
      inLatinWord = false;
    }
  }
  return Math.max(1, units);
}

function hardSplitByReadingUnits(
  text: string,
  language: string,
  maxUnits: number,
): string[] {
  const tokens = isSpaceSeparatedLanguage(language)
    ? (text.match(/\S+(?:\s+|$)/gu) ?? [text])
    : graphemes(text, language);
  const chunks: string[] = [];
  let pending = "";
  let pendingUnits = 0;

  for (const token of tokens) {
    const tokenUnits = readingUnits(token, language);
    if (pending && pendingUnits + tokenUnits > maxUnits) {
      chunks.push(pending.trim());
      pending = "";
      pendingUnits = 0;
    }
    pending += token;
    pendingUnits += tokenUnits;
  }
  if (pending.trim()) {
    chunks.push(pending.trim());
  }
  return chunks;
}

function splitForReadingPressure(
  cues: readonly RawCue[],
  caption: CaptionCueInput,
  profile: CaptionLayoutProfile,
): RawCue[] {
  const start = caption.audioStartMs;
  const end = caption.audioEndMs;
  if (start === null || end === null || end <= start) {
    return [...cues];
  }

  const durationSeconds = (end - start) / 1_000;
  const totalUnits = cues.reduce(
    (total, cue) => total + readingUnits(cue.text, caption.language),
    0,
  );
  if (totalUnits / durationSeconds <= profile.maxReadingUnitsPerSecond) {
    return [...cues];
  }

  const maxUnits = Math.max(
    1,
    Math.floor(profile.maxReadingUnitsPerSecond * durationSeconds),
  );
  return cues.flatMap((cue) => {
    if (readingUnits(cue.text, caption.language) <= maxUnits) {
      return [cue];
    }
    const semanticPieces = cue.text.match(
      /[^。！？!?；;：:，,、]+[。！？!?；;：:，,、]?/gu,
    ) ?? [cue.text];
    const chunks: string[] = [];
    let pending = "";
    let pendingUnits = 0;

    const pushPending = () => {
      if (pending.trim()) {
        chunks.push(pending.trim());
      }
      pending = "";
      pendingUnits = 0;
    };

    for (const piece of semanticPieces) {
      const pieceUnits = readingUnits(piece, caption.language);
      if (pieceUnits > maxUnits) {
        pushPending();
        chunks.push(
          ...hardSplitByReadingUnits(piece, caption.language, maxUnits),
        );
        continue;
      }
      if (pending && pendingUnits + pieceUnits > maxUnits) {
        pushPending();
      }
      pending = joinText(pending, piece, caption.language);
      pendingUnits += pieceUnits;
    }
    pushPending();

    return chunks.flatMap((chunk) =>
      rawCuesForSpan(chunk, caption.language, profile),
    );
  });
}

function allocateCueTimes(
  cues: readonly RawCue[],
  caption: CaptionCueInput,
): Array<{ audioStartMs: number | null; audioEndMs: number | null }> {
  const start = caption.audioStartMs;
  const end = caption.audioEndMs;
  if (start === null || end === null || end < start || cues.length === 0) {
    return cues.map(() => ({ audioStartMs: start, audioEndMs: end }));
  }

  const weights = cues.map((cue) => readingUnits(cue.text, caption.language));
  const totalWeight = weights.reduce((total, value) => total + value, 0);
  const duration = end - start;
  let consumedWeight = 0;

  return weights.map((weight, index) => {
    const cueStart = index === 0
      ? start
      : Math.round(start + duration * (consumedWeight / totalWeight));
    consumedWeight += weight;
    const cueEnd = index === weights.length - 1
      ? end
      : Math.round(start + duration * (consumedWeight / totalWeight));
    return { audioStartMs: cueStart, audioEndMs: cueEnd };
  });
}

export function composeCaptionCues(
  captions: readonly CaptionCueInput[],
  profile: CaptionLayoutProfile,
): CaptionDisplayCue[] {
  validateProfile(profile);

  return captions.flatMap((caption, captionIndex) => {
    const normalized = normalizeText(caption.text);
    if (!normalized) {
      return [];
    }

    const rawCues = sentenceSpans(normalized).flatMap((sentence) =>
      rawCuesForSpan(sentence, caption.language, profile),
    );
    const mergedCues = mergeShortCues(rawCues, caption.language, profile);
    const pressureSplitCues = splitForReadingPressure(
      mergedCues,
      caption,
      profile,
    );
    const times = allocateCueTimes(pressureSplitCues, caption);

    return pressureSplitCues.map((cue, cueIndex) => {
      return {
        ...caption,
        displayId:
          `${caption.segmentId}:display:${captionIndex + 1}:${cueIndex + 1}`,
        text: cue.text,
        lines: cue.lines,
        audioStartMs: times[cueIndex]?.audioStartMs ?? caption.audioStartMs,
        audioEndMs: times[cueIndex]?.audioEndMs ?? caption.audioEndMs,
      };
    });
  });
}
