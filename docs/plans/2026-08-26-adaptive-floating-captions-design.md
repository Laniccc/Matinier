# Adaptive Floating Captions Design

## Goal

Improve live-caption readability in two connected ways:

1. Replace fixed character-count display splitting with language-aware,
   width-aware, and time-aware caption cue composition.
2. Let a viewer open an always-on-top caption window while watching video in
   another tab or application.

The floating window defaults to translated Draft captions and replaces them
smoothly with Final captions. Source captions and bilingual display remain
available as explicit user choices.

## Approved scope

- Target desktop Chrome and Edge first.
- Use the Document Picture-in-Picture API for an always-on-top web window.
- Keep existing ASR and translation events, persistence, evidence IDs, and
  LiveKit connections unchanged.
- Perform all cue composition in the frontend presentation layer.
- Default the floating window to translation-only display.
- Show low-latency Draft text, then replace it with Final text.
- Do not add a normal popup fallback because it would not reliably stay on top.
- Do not build an Electron or Tauri desktop shell in this version.

## Current behavior and problem

The frontend currently calls `splitFinalsForDisplay` with fixed limits of 54
characters for source text and 96 characters for translated text. It first
splits on a small set of sentence punctuation and then falls back to whitespace
or a hard character boundary. This correctly preserves persisted provider
Finals, but it does not account for the visible width, selected font size,
language, reading pressure, short fragments, or a two-line subtitle layout.

All captions are rendered inside the Studio page. A viewer must therefore keep
the control page visible instead of focusing on the video or another
application.

## Alternatives considered

### Caption composition

1. **Presentation-layer adaptive cue composition — selected.** It improves
   both source and translated display while leaving stored truth and evidence
   references intact.
2. **Provider-level endpoint tuning.** Silence and sentence-boundary settings
   may improve ASR segments, but they are provider-specific, alter persisted
   segment boundaries, and cannot guarantee readable translated captions.
3. **LLM semantic segmentation.** It may produce better semantic boundaries,
   but adds latency, cost, and non-determinism to the realtime path.

Provider endpoint tuning may be evaluated later as a complementary change. It
is not part of this feature.

### Floating window

1. **Document Picture-in-Picture — selected.** It provides an always-on-top
   arbitrary HTML window that can share the current page state.
2. **Electron or Tauri overlay.** It could provide a borderless, transparent,
   click-through lyric overlay, but would introduce desktop packaging and a
   second runtime form.
3. **Regular browser popup.** It is more widely available but cannot satisfy
   the always-on-top requirement.

The selected browser solution will not provide a fully borderless or
click-through native overlay. The rendering layer should remain independent so
it can be reused in a desktop shell if that becomes necessary.

## Architecture

Existing event decoding and caption state remain authoritative. A new pure
presentation layer derives display cues for both the Studio transcript and the
floating view.

```text
LiveKit realtime events / HTTP Final snapshots
                    |
                    v
             existing CaptionState
                    |
                    v
             CaptionCueComposer
              /             \
             v               v
    Studio transcript   floating caption view
```

### CaptionCueComposer

The composer owns no network, persistence, or React state. It accepts caption
text, language, timestamps, a layout profile, and an injected text-width
measurer. It exposes separate composition paths for historical Finals and the
current realtime cue while sharing the same boundary-selection rules.

Its output carries presentation IDs while retaining the original segment ID,
revision, language, and audio range. Splitting a Final therefore never creates
new domain segments or evidence IDs.

### FloatingCaptionController

The controller owns the Document Picture-in-Picture lifecycle:

- support detection;
- opening in direct response to a user action;
- reusing or focusing an existing window;
- synchronizing external window closure with the Studio toggle;
- releasing a stale window or Portal target safely; and
- persisting view preferences in browser storage.

The controller does not create a second LiveKit connection. The floating view
reads the same React caption state as the Studio.

### FloatingCaptionView

The view renders only caption presentation and compact controls. It has no
knowledge of LiveKit or persistence. It is rendered into the Picture-in-Picture
document and receives already-composed cues plus display preferences.

## Adaptive cue composition

### Inputs

The composition profile includes:

- source or target language;
- available content width;
- font family and font size;
- maximum two lines per cue;
- optional audio start and end timestamps; and
- Draft or Final status.

### Boundary selection

The composer applies these rules in order:

1. Normalize repeated whitespace while retaining meaningful punctuation and
   line breaks.
2. Prefer sentence boundaries: newline, full stop, question mark, and
   exclamation mark.
3. For a sentence that still exceeds the two-line capacity, prefer clause
   boundaries such as semicolon, colon, comma, and enumeration comma.
4. Split English-like text on word boundaries and CJK text on grapheme
   boundaries. Do not cut an English word, number, or emoji cluster.
5. Continue oversized content in a later cue.
6. Merge a very short fragment with an adjacent compatible fragment when the
   combined cue fits.
7. Use the available audio range to detect excessive reading pressure. Split
   overloaded text further, and merge short adjacent text when the gap and
   combined duration permit it.

The composer measures rendered width rather than applying one character limit
to every language. Initial visual tuning should aim around 16–20 CJK
characters or 32–42 Latin characters per line at the default floating size,
but these are calibration targets rather than hard domain limits.

### Draft stability

Draft captions need low latency without visible reflow on every provider
revision:

- coalesce visual refreshes to at most once every roughly 100–150 ms;
- update only the active cue rather than appending a growing transcript;
- preserve an already-stable first line while normal text is appended to the
  last line;
- create a new cue only at a strong boundary, a clear clause boundary, or
  actual two-line overflow; and
- run full composition once when the Final revision arrives, then replace the
  Draft with a short fade.

A Final receives a minimum readable hold, but the UI does not build an
unbounded playback queue. When newer live speech has arrived, stale queued
presentation is dropped so captions remain current.

### Transcript and floating profiles

The Studio transcript and floating window use the same composition engine with
different measured widths. Resizing the floating window recomposes presentation
cues only; it does not modify caption state or timestamps.

## Floating-window interaction

Add a `悬浮字幕` toggle to the live-caption section header. Checking it opens
the Picture-in-Picture window in the required user gesture. Closing either the
toggle or the external window synchronizes the other state.

The initial requested size is approximately 720 by 180 pixels, subject to
browser clamping. Browser-managed placement and size memory are used instead of
attempting to position the window from the page.

Default presentation:

- translation only;
- at most two lines;
- dark translucent background;
- light foreground text with a subtle shadow;
- no timestamps, confidence values, revision labels, or Draft/Final badges;
- no animation for ordinary Draft increments; and
- an approximately 150 ms fade when Final replaces Draft.

On pointer hover or keyboard focus, a compact toolbar exposes:

- translated, source, or bilingual mode;
- smaller and larger font controls;
- background opacity; and
- close.

The toolbar hides when inactive. In bilingual mode, source text is visually
secondary and translated text remains primary.

### Language and translation states

- Before the first translation appears, show `正在等待译文…`.
- If the Session has no target language, show source captions and explain that
  translation is not enabled.
- If translation fails while translation mode is active, fall back to source
  captions and briefly show `翻译中断，已切换原文`.
- A manual source-language choice is sticky and is not overridden if
  translation later recovers.

User preferences for language mode, font size, and background opacity are
stored locally. They do not affect other caption consumers.

## Error handling and lifecycle

- If the API is unavailable, disabled by browser policy, or denied, restore
  the toggle to off and show a specific user-facing reason.
- Unsupported browsers do not receive a regular popup fallback. The UI asks
  the user to use desktop Chrome or Edge.
- Only one floating window may exist for the Studio page. A repeated open action
  reuses or focuses it.
- Closing the Picture-in-Picture window releases references and stops Portal
  rendering without affecting the audio input or caption run.
- The floating window cannot outlive its opener. Refreshing or closing the
  Studio closes it; the user opens it again after returning.
- A React update racing with external-window closure is treated as a harmless
  view teardown, not a page failure.
- Empty text and out-of-order events continue to rely on the existing
  `segment_id + revision` reconciliation rules.

## Verification

### Unit tests

Add deterministic tests around the pure composer with an injected width
measurer. Cover:

- Chinese and English sentence punctuation;
- long text without punctuation;
- clause-boundary preference;
- merging undersized fragments;
- English words, numbers, and emoji graphemes;
- two-line capacity at different widths and font sizes;
- time-pressure splitting and compatible merging; and
- stable Draft-to-Final replacement.

Test the pure floating state transitions for open, close, repeated open,
unsupported browser, translation failure fallback, and sticky manual language
selection.

### Browser acceptance

Run a real Chinese-to-English caption session in desktop Chrome and Edge:

1. Open the floating view by checking the Studio toggle.
2. Switch to a video tab and another desktop application and verify the window
   remains on top.
3. Confirm Draft translation appears promptly and Final replaces it smoothly.
4. Resize the window and confirm cues recompose without overflow or data
   changes.
5. Exercise translated, source, and bilingual modes plus font and opacity
   controls.
6. Simulate translation failure and confirm source fallback.
7. Close the external window and confirm the caption run continues.

Finish with frontend type checking and a production build. Operating-system
always-on-top behavior is a manual acceptance item because headless browser
tests cannot validate it reliably.

## Non-goals

- Changing ASR or translation provider protocols and endpointing.
- Persisting Draft captions or presentation cue boundaries.
- Replacing stored Final segments with display fragments.
- Adding backend APIs, database migrations, or a second realtime connection.
- Building a native transparent or click-through desktop overlay.
- Supporting mobile browsers or all desktop browser engines in this version.
