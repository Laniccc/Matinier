# Adaptive Floating Captions Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace fixed-length caption splitting with adaptive, readable cues and add a translation-first, always-on-top caption window for desktop Chrome and Edge.

**Architecture:** Keep the existing `CaptionState`, LiveKit event flow, snapshots, persistence, and evidence IDs authoritative. Add pure frontend cue-composition and floating-view state modules, use them from both the Studio transcript and a React Portal rendered into a Document Picture-in-Picture window, and keep all provider and backend contracts unchanged.

**Tech Stack:** Next.js 16, React 19, TypeScript 6, Vitest, browser Canvas text measurement, ResizeObserver, React Portal, Document Picture-in-Picture API.

---

## Before implementation

Read the approved design at
`docs/plans/2026-08-26-adaptive-floating-captions-design.md` before editing.

The workspace currently contains a `.git` directory without `HEAD`, so Git does
not recognize it as a repository. The commit steps below assume the original
repository metadata has been restored. Do not run `git init` to fabricate new
history. If metadata is still unavailable, execute and verify each task but
record that its commit step was skipped.

Run all frontend commands from `frontend/`:

```powershell
pnpm typecheck
pnpm build
```

Expected baseline: both commands exit successfully before feature work begins.

## Shared implementation contracts

Use these public types throughout the plan. Small naming improvements are
allowed, but do not widen their responsibilities.

```ts
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
```

The composer must be deterministic for the same input and measurer. It must not
read the DOM, current time, React state, or browser storage.

### Task 1: Add a test runner and the first width-aware cue

**Files:**

- Modify: `frontend/package.json`
- Modify: `frontend/pnpm-lock.yaml`
- Create: `frontend/lib/caption-cues.ts`
- Create: `frontend/lib/caption-cues.test.ts`

**Step 1: Install the test runner**

Run:

```powershell
pnpm add --save-dev vitest
```

Expected: `vitest` is recorded in `devDependencies` and the lockfile changes.

Add this package script:

```json
"test": "vitest run"
```

**Step 2: Write the first failing tests**

Create `frontend/lib/caption-cues.test.ts` with explicit Vitest imports and a
deterministic fake measurer:

```ts
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
    const cues = composeCaptionCues([input("这是一句没有停顿而且非常长的测试文字")], profile);

    expect(
      cues.flatMap((cue) => cue.lines)
        .every((line) => measureText(line) <= 120),
    ).toBe(true);
  });
});
```

**Step 3: Run the tests and verify failure**

Run:

```powershell
pnpm test -- lib/caption-cues.test.ts
```

Expected: FAIL because `./caption-cues` does not exist.

**Step 4: Implement the minimal public module**

Create `frontend/lib/caption-cues.ts` with the shared types above and these
exports:

```ts
export function composeCaptionCues(
  captions: readonly CaptionCueInput[],
  profile: CaptionLayoutProfile,
): CaptionDisplayCue[];
```

For the first pass:

- validate `maxLineWidthPx > 0`, `maxLines` is 1 or 2, and `measureText` returns
  non-negative finite widths;
- normalize repeated horizontal whitespace;
- tokenize complete sentences while retaining `。！？!?` and newlines;
- greedily wrap each sentence into at most two measured lines;
- if no boundary fits, split at a grapheme boundary using `Intl.Segmenter` with
  an `Array.from` fallback; and
- assign stable IDs in the form
  `${segmentId}:display:${captionIndex + 1}:${cueIndex + 1}`.

Do not add timing allocation, short-fragment merging, or Draft stability yet.

**Step 5: Run focused tests**

Run:

```powershell
pnpm test -- lib/caption-cues.test.ts
```

Expected: PASS, 2 tests.

**Step 6: Run static validation**

Run:

```powershell
pnpm typecheck
```

Expected: exit code 0.

**Step 7: Commit**

```powershell
git add frontend/package.json frontend/pnpm-lock.yaml frontend/lib/caption-cues.ts frontend/lib/caption-cues.test.ts
git commit -m "test: add adaptive caption cue foundation"
```

### Task 2: Complete language-aware boundary selection and merging

**Files:**

- Modify: `frontend/lib/caption-cues.ts`
- Modify: `frontend/lib/caption-cues.test.ts`

**Step 1: Add failing clause and token-boundary tests**

Add focused cases that assert:

```ts
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

  expect(cues.flatMap((cue) => cue.lines).join(" ")).not.toContain("subti tles");
  expect(cues.map((cue) => cue.text).join(" ")).toContain("subtitles");
});

it("keeps an emoji grapheme intact", () => {
  const cues = composeCaptionCues(
    [input("现在开始测试👨‍👩‍👧‍👦然后继续说明")],
    { ...profile, maxLineWidthPx: 80 },
  );

  expect(cues.map((cue) => cue.text).join("")).toContain("👨‍👩‍👧‍👦");
});

it("merges a short fragment with an adjacent sentence when both fit", () => {
  const cues = composeCaptionCues(
    [input("好。下面开始介绍主要内容。")],
    { ...profile, maxLineWidthPx: 180 },
  );

  expect(cues[0]?.text).toBe("好。下面开始介绍主要内容。");
});
```

Also add cases for `；;：:，,、`, repeated spaces, embedded newlines,
numbers such as `2026-08-26`, empty input, and an invalid layout profile.

**Step 2: Run the tests and verify failure**

Run:

```powershell
pnpm test -- lib/caption-cues.test.ts
```

Expected: at least the comma-preference and short-fragment tests fail.

**Step 3: Implement prioritized break candidates**

Inside `caption-cues.ts`, keep helpers private and implement this priority:

```ts
const STRONG_BOUNDARY = /[。！？!?\n]/u;
const CLAUSE_BOUNDARY = /[；;：:，,、]/u;
```

For each oversized span:

1. enumerate grapheme-safe candidate indexes;
2. retain candidates whose prefix fits the remaining measured width;
3. choose the last strong boundary, otherwise the last clause boundary,
   otherwise the last word boundary for whitespace languages, otherwise the
   last fitting grapheme;
4. retain the punctuation on the preceding span; and
5. trim only boundary whitespace, never meaningful punctuation.

Implement short-fragment merging as a second deterministic pass. A cue is
eligible when its measured width is below
`maxLineWidthPx * maxLines * minFillRatio`; merge it with the next cue first,
then the previous cue, only when the result still wraps into `maxLines`.

**Step 4: Run focused tests**

Run:

```powershell
pnpm test -- lib/caption-cues.test.ts
```

Expected: all boundary, word, number, emoji, whitespace, merge, and validation
tests pass.

**Step 5: Commit**

```powershell
git add frontend/lib/caption-cues.ts frontend/lib/caption-cues.test.ts
git commit -m "feat: compose language-aware caption cues"
```

### Task 3: Add time allocation and stable live-cue state

**Files:**

- Modify: `frontend/lib/caption-cues.ts`
- Modify: `frontend/lib/caption-cues.test.ts`
- Create: `frontend/lib/live-caption-view.ts`
- Create: `frontend/lib/live-caption-view.test.ts`

**Step 1: Write failing time-allocation tests**

Add a long Final with a known `0..6_000` ms range and assert:

```ts
const cues = composeCaptionCues(
  [{
    ...input("第一部分内容较长，需要拆分。第二部分也需要足够阅读时间。"),
    audioEndMs: 6_000,
  }],
  { ...profile, maxLineWidthPx: 100 },
);

expect(cues[0]?.audioStartMs).toBe(0);
expect(cues.at(-1)?.audioEndMs).toBe(6_000);
expect(cues.every((cue, index) =>
  index === 0 || cue.audioStartMs === cues[index - 1]?.audioEndMs
)).toBe(true);
```

Add a case where a cue exceeds `maxReadingUnitsPerSecond` for its allocated
duration and must split again, plus a null-timestamp case that remains null.

**Step 2: Run and verify failure**

Run:

```powershell
pnpm test -- lib/caption-cues.test.ts
```

Expected: timing assertions fail because Task 2 does not allocate cue ranges.

**Step 3: Implement proportional contiguous ranges**

Allocate the source range across composed cues by reading units. Count each CJK
grapheme as one unit and each whitespace-delimited Latin word as one unit. The
first cue starts at the original start, the last cue ends at the original end,
and intermediate boundaries are rounded while remaining monotonic. Do not
invent times when either endpoint is null or invalid.

Before final allocation, split a cue again when its reading units divided by
its provisional duration exceeds `maxReadingUnitsPerSecond` and a safe boundary
exists.

**Step 4: Write failing live-state tests**

Create `frontend/lib/live-caption-view.test.ts` for a pure state transition API:

```ts
import { describe, expect, it } from "vitest";

import {
  advanceLiveCaptionView,
  createLiveCaptionViewState,
} from "./live-caption-view";

it("coalesces Draft changes inside the 125 ms refresh window", () => {
  const initial = createLiveCaptionViewState();
  const first = advanceLiveCaptionView(initial, {
    candidate: { key: "translation:1:1", text: "Hello", status: "draft" },
    nowMs: 1_000,
  });
  const second = advanceLiveCaptionView(first.state, {
    candidate: { key: "translation:1:2", text: "Hello world", status: "draft" },
    nowMs: 1_050,
  });

  expect(second.state.visible?.text).toBe("Hello");
  expect(second.state.pending?.text).toBe("Hello world");
  expect(second.nextUpdateAtMs).toBe(1_125);
});

it("lets Final replace the matching Draft", () => {
  // Use the same segment key with a higher revision and status: "final".
  // Assert visible status becomes final and transition is "final-replace".
});

it("does not build an unbounded queue behind newer speech", () => {
  // Feed several stale Finals, then a newer Draft.
  // Assert only the newest pending candidate remains.
});
```

**Step 5: Run and verify failure**

Run:

```powershell
pnpm test -- lib/live-caption-view.test.ts
```

Expected: FAIL because `live-caption-view.ts` does not exist.

**Step 6: Implement the pure live state machine**

Use these minimum contracts:

```ts
export type LiveCaptionCandidate = {
  key: string;
  segmentId: string;
  revision: number;
  status: "draft" | "final";
  text: string;
};

export type LiveCaptionViewState = {
  visible: LiveCaptionCandidate | null;
  pending: LiveCaptionCandidate | null;
  lastRefreshAtMs: number | null;
  finalHoldUntilMs: number | null;
  transition: "none" | "final-replace";
};

export function createLiveCaptionViewState(): LiveCaptionViewState;

export function advanceLiveCaptionView(
  previous: LiveCaptionViewState,
  input: {
    candidate: LiveCaptionCandidate | null;
    nowMs: number;
    refreshIntervalMs?: number;
    minimumFinalHoldMs?: number;
  },
): { state: LiveCaptionViewState; nextUpdateAtMs: number | null };
```

Default Draft refresh interval: 125 ms. Default minimum Final hold: 1,200 ms.
A newer Draft may replace stale held content when otherwise the view would fall
behind. Keep only one pending candidate.

**Step 7: Run all pure tests**

Run:

```powershell
pnpm test
pnpm typecheck
```

Expected: all tests pass and TypeScript exits with code 0.

**Step 8: Commit**

```powershell
git add frontend/lib/caption-cues.ts frontend/lib/caption-cues.test.ts frontend/lib/live-caption-view.ts frontend/lib/live-caption-view.test.ts
git commit -m "feat: stabilize realtime caption presentation"
```

### Task 4: Replace fixed transcript splitting in the Studio

**Files:**

- Create: `frontend/hooks/use-element-width.ts`
- Create: `frontend/lib/caption-measurement.ts`
- Modify: `frontend/lib/captions.ts:503-603`
- Modify: `frontend/components/room-studio.tsx:9-54`
- Modify: `frontend/components/room-studio.tsx:271-310`
- Modify: `frontend/components/room-studio.tsx:1387-1490`

**Step 1: Add browser measurement helpers**

Create `frontend/lib/caption-measurement.ts`:

```ts
import type { TextWidthMeasurer } from "./caption-cues";

export function createCanvasTextMeasurer(font: string): TextWidthMeasurer {
  const canvas = document.createElement("canvas");
  const context = canvas.getContext("2d");
  const cache = new Map<string, number>();

  if (context === null) {
    return (text) => Array.from(text).length;
  }
  context.font = font;
  return (text) => {
    const cached = cache.get(text);
    if (cached !== undefined) return cached;
    const width = context.measureText(text).width;
    cache.set(text, width);
    return width;
  };
}
```

Keep the cache bounded, for example by clearing it at 1,000 entries.

Create `frontend/hooks/use-element-width.ts` using `ResizeObserver`. It accepts
an element ref and a positive fallback width, reports `contentRect.width`, and
disconnects on cleanup.

**Step 2: Integrate adaptive Finals**

In `room-studio.tsx`:

- add refs to the source and translation caption lanes;
- measure each lane width and subtract its transcript padding/time columns;
- memoize Canvas measurers for the actual caption font;
- map existing `CaptionPayload` and `TranslationPayload` into
  `CaptionCueInput` without losing IDs, revisions, language, or timestamps;
- replace `splitFinalsForDisplay(finals, 54)` and
  `splitFinalsForDisplay(translationFinals, 96)` with
  `composeCaptionCues(..., measuredProfile)`; and
- render composed IDs, text, and allocated start time in the existing lists.

Use a two-line profile and the current lane width. Do not hard-code replacement
character limits.

Apply the same composer to current source and translation Draft text before it
is rendered in `.liveDraftArea`; show only the active one or two presentation
lines per provider Draft.

**Step 3: Remove obsolete fixed splitting**

Delete `boundedTextChunks`, `DisplayCaption`, and `splitFinalsForDisplay` from
`frontend/lib/captions.ts` once no imports remain. Do not change event decoding,
snapshot hydration, or the reducer.

**Step 4: Run regression validation**

Run:

```powershell
pnpm test
pnpm typecheck
pnpm build
```

Expected: all tests pass; typecheck and production build exit successfully;
`rg "splitFinalsForDisplay|boundedTextChunks" frontend` finds no obsolete
display splitter.

**Step 5: Commit**

```powershell
git add frontend/hooks/use-element-width.ts frontend/lib/caption-measurement.ts frontend/lib/captions.ts frontend/components/room-studio.tsx
git commit -m "feat: adapt Studio captions to visible width"
```

### Task 5: Implement preference resolution and Picture-in-Picture lifecycle

**Files:**

- Create: `frontend/types/document-picture-in-picture.d.ts`
- Create: `frontend/lib/floating-caption-state.ts`
- Create: `frontend/lib/floating-caption-state.test.ts`
- Create: `frontend/lib/floating-caption-window.ts`
- Create: `frontend/lib/floating-caption-window.test.ts`

**Step 1: Declare the missing browser API types**

TypeScript's current DOM library in this project does not declare Document
Picture-in-Picture. Add a minimal global declaration containing only the fields
used by the feature:

```ts
type DocumentPictureInPictureOptions = {
  width?: number;
  height?: number;
  disallowReturnToOpener?: boolean;
  preferInitialWindowPlacement?: boolean;
};

interface DocumentPictureInPicture {
  readonly window: Window | null;
  requestWindow(options?: DocumentPictureInPictureOptions): Promise<Window>;
}

interface Window {
  readonly documentPictureInPicture?: DocumentPictureInPicture;
}
```

Do not use `any` in the component to bypass the API boundary.

**Step 2: Write failing preference tests**

Define modes `translation | source | bilingual` and test:

- default preference is translation;
- a Session without a target language resolves to source with the notice
  `当前任务未启用翻译`;
- failed translation resolves an automatic translation preference to source
  with `翻译中断，已切换原文`;
- a user-selected source preference remains source after translation recovers;
- malformed persisted JSON returns defaults; and
- font size and background opacity are clamped to supported ranges.

Use a storage adapter interface rather than reading `localStorage` directly in
the pure module.

**Step 3: Run and verify failure**

Run:

```powershell
pnpm test -- lib/floating-caption-state.test.ts
```

Expected: FAIL because the state module does not exist.

**Step 4: Implement preference state**

Use a versioned storage payload and these defaults:

```ts
export const DEFAULT_FLOATING_CAPTION_PREFERENCES = {
  version: 1,
  mode: "translation",
  modeWasSelectedByUser: false,
  fontSizePx: 32,
  backgroundOpacity: 0.68,
} as const;
```

Clamp font size to 20–56 px and opacity to 0.2–0.95. Persist only preference
fields, never caption text, Session IDs, or translation errors.

**Step 5: Write failing window lifecycle tests**

In `floating-caption-window.test.ts`, inject a narrow host interface and assert:

- unsupported hosts produce error code `unsupported`;
- an existing open window is focused and reused;
- `requestWindow` receives `{ width: 720, height: 180 }`;
- `NotAllowedError` maps to code `not-allowed`;
- a disabled API maps to `not-supported`; and
- an arbitrary rejection maps to `open-failed` without leaking raw objects.

**Step 6: Implement the lifecycle adapter**

Export:

```ts
export type FloatingCaptionWindowErrorCode =
  | "unsupported"
  | "not-allowed"
  | "not-supported"
  | "open-failed";

export async function requestFloatingCaptionWindow(
  host: Window,
): Promise<{ window: Window; reused: boolean }>;
```

The function must be called directly from the toggle event handler. Do not
defer the request to `useEffect`, because the browser requires transient user
activation.

**Step 7: Run pure tests and typecheck**

Run:

```powershell
pnpm test -- lib/floating-caption-state.test.ts lib/floating-caption-window.test.ts
pnpm typecheck
```

Expected: all tests pass and TypeScript exits with code 0.

**Step 8: Commit**

```powershell
git add frontend/types/document-picture-in-picture.d.ts frontend/lib/floating-caption-state.ts frontend/lib/floating-caption-state.test.ts frontend/lib/floating-caption-window.ts frontend/lib/floating-caption-window.test.ts
git commit -m "feat: add floating caption browser lifecycle"
```

### Task 6: Build and connect the floating caption view

**Files:**

- Create: `frontend/components/floating-captions.tsx`
- Modify: `frontend/components/room-studio.tsx:303-310`
- Modify: `frontend/components/room-studio.tsx:1361-1375`
- Modify: `frontend/app/globals.css:1713-1880`

**Step 1: Define a narrow component boundary**

The new client component receives already-reconciled arrays and status:

```ts
type FloatingCaptionsProps = {
  sourceLanguage: string;
  targetLanguage: string | null;
  sourceDrafts: readonly CaptionPayload[];
  sourceFinals: readonly CaptionPayload[];
  translationDrafts: readonly TranslationPayload[];
  translationFinals: readonly TranslationPayload[];
  translationStatus: TranslationStatus;
};
```

Do not pass the LiveKit `Room`, repositories, API clients, or input controls.

**Step 2: Open the external document safely**

On the toggle's `onChange`/`onClick` handler:

1. call `requestFloatingCaptionWindow(window)` synchronously from the user
   event;
2. set the returned document title to `Matinier 悬浮字幕`;
3. clone current `link[rel="stylesheet"]` and `style` elements into the Picture-
   in-Picture head so both production CSS links and development style tags work;
4. assign `floatingCaptionDocument` to the external document element/body and
   create one root element with class `floatingCaptionRoot`;
5. render the floating UI with `createPortal` from `react-dom`;
6. listen for `pagehide` in the external window and synchronize the toggle to
   off; and
7. remove listeners and Portal state during cleanup.

Guard every update with `pipWindow.closed`. External closure is a normal view
teardown, not an application error.

**Step 3: Select and stabilize visible text**

For the effective language mode:

- choose the newest Draft by audio start, received time, segment ID, and
  revision;
- if no Draft exists, choose the newest Final;
- feed the candidate through `advanceLiveCaptionView`;
- schedule at most one timer for `nextUpdateAtMs`;
- compose the visible candidate using the current Picture-in-Picture inner
  width minus horizontal padding;
- on resize, recompose display lines but retain the domain candidate; and
- add class `floatingCaptionText-finalReplace` only for the Final replacement
  transition.

Translation mode with no translated text renders `正在等待译文…`. Resolve
disabled or failed translation through `floating-caption-state.ts`.

**Step 4: Render accessible controls**

Add a toolbar that appears on hover and `:focus-within` and contains:

- three mode buttons labelled `译文`, `原文`, and `双语`;
- `减小字号` and `增大字号` buttons;
- a labelled opacity range input; and
- a `关闭悬浮字幕` button.

The floating content uses `aria-live="polite"` and `aria-atomic="true"`. The
Studio toggle uses a native checkbox or `role="switch"` with a correct checked
state. Do not render time, confidence, revision, LIVE, or FINAL labels inside
the floating window.

**Step 5: Add styles**

Add Studio classes for the switch and support/error hint, plus external-window
classes:

```css
.floatingCaptionDocument,
.floatingCaptionRoot {
  width: 100%;
  min-height: 100%;
  margin: 0;
}

.floatingCaptionRoot {
  display: grid;
  place-items: center;
  padding: 20px 24px;
  overflow: hidden;
  background: rgba(5, 10, 18, var(--floating-caption-opacity));
}

.floatingCaptionText {
  max-width: 100%;
  margin: 0;
  overflow: hidden;
  color: #fff;
  font-size: var(--floating-caption-font-size);
  font-weight: 700;
  line-height: 1.3;
  text-align: center;
  text-wrap: balance;
  text-shadow: 0 2px 8px #000, 0 0 2px #000;
}
```

Use CSS custom properties set on the root for font size and opacity. Add a
150 ms opacity animation only for `.floatingCaptionText-finalReplace`. Ensure
the toolbar does not obscure text at 720×180 and reflows at narrower widths.

**Step 6: Integrate into RoomStudio**

Place `<FloatingCaptions>` in `.captionStageHeader` beside the existing counters
and pass the selected arrays and languages. The toggle remains available before
a run starts, but the external view shows an idle/waiting message until caption
text exists.

Closing the Picture-in-Picture view must not call `stopInput`, `cancelCaptionRun`,
`disconnectCurrentRoom`, or any API method.

**Step 7: Run automated validation**

Run:

```powershell
pnpm test
pnpm typecheck
pnpm build
```

Expected: all unit tests pass; typecheck and build exit with code 0; the build
does not attempt to access `window`, Canvas, localStorage, or Picture-in-Picture
during server rendering.

**Step 8: Commit**

```powershell
git add frontend/components/floating-captions.tsx frontend/components/room-studio.tsx frontend/app/globals.css
git commit -m "feat: add always-on-top floating captions"
```

### Task 7: Complete manual browser acceptance and documentation

**Files:**

- Modify: `README.md:327-348`
- Modify: `docs/stage-records.md`
- Modify as needed after defects: files touched in Tasks 1–6

**Step 1: Start the existing demo**

From the repository root, use the existing launcher:

```powershell
.\start_demo.cmd
```

Expected: API and Next.js Studio start without new warnings or crashes. Use a
desktop Chrome or Edge build with Document Picture-in-Picture support.

**Step 2: Accept adaptive Studio captions**

Run a speech-bearing Chinese-to-English Session and verify:

- long source and translated Finals wrap into readable one- or two-line cues;
- sentence punctuation is preferred over hard breaks;
- short fragments are merged when they fit;
- English words, numbers, and emoji are intact;
- resizing the browser recomposes display cues; and
- the persisted Session segment counts and evidence IDs remain unchanged.

**Step 3: Accept the always-on-top flow**

Check `悬浮字幕` and verify:

- the window opens only from the user action and stays above a video tab and a
  separate desktop application;
- translation is the default;
- Draft updates are prompt without per-character animation;
- matching Final text replaces Draft with one short fade;
- the window can be dragged and resized and lines recompose without overflow;
- source, translation, and bilingual modes work;
- font and opacity preferences survive closing and reopening;
- closing the external window synchronizes the Studio switch; and
- the active caption run continues after the external window closes.

**Step 4: Exercise error and fallback states**

Verify in a real browser or with controlled state:

- a Session without translation displays source and `当前任务未启用翻译`;
- translation failure displays source and the brief fallback notice;
- a manual source choice is not overwritten after recovery;
- unsupported browsers keep the switch off and request desktop Chrome/Edge;
- denied Picture-in-Picture permission produces an actionable message; and
- repeated open actions do not create multiple windows.

**Step 5: Document the user flow**

Update the README demo section with:

- supported browsers;
- how to check and close `悬浮字幕`;
- translation-first behavior and language controls;
- the fact that the opener page must remain open; and
- the absence of native borderless/click-through behavior.

Add an acceptance record to `docs/stage-records.md` containing the browser and
version, source type, language pair, pass/fail result, and any known visual
limitations. Do not record credentials, signed URLs, or raw provider payloads.

**Step 6: Run final validation**

Run:

```powershell
Push-Location frontend
pnpm test
pnpm typecheck
pnpm build
Pop-Location
```

Expected: all tests pass, typecheck exits 0, and Next.js reports a successful
production build.

Review the diff:

```powershell
git diff --check
git status --short
```

Expected: no whitespace errors; only the planned frontend files and approved
documentation are modified.

**Step 7: Commit**

```powershell
git add README.md docs/stage-records.md frontend
git commit -m "docs: record floating caption acceptance"
```

## Completion criteria

The feature is complete only when all of the following are true:

- no fixed 54/96-character display splitter remains;
- both Studio Draft/Final text and the floating view use the adaptive composer;
- persisted source and translation Finals are unchanged;
- the floating view defaults to translation and shows Draft before Final;
- closing the floating window never changes the caption-run lifecycle;
- unit tests, typecheck, and production build pass; and
- a real Chrome/Edge acceptance run confirms operating-system always-on-top
  behavior.
