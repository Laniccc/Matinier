import { describe, expect, it } from "vitest";

import {
  advanceLiveCaptionView,
  createLiveCaptionViewState,
  type LiveCaptionCandidate,
} from "./live-caption-view";

function candidate(
  text: string,
  overrides: Partial<LiveCaptionCandidate> = {},
): LiveCaptionCandidate {
  return {
    key: "translation:segment-1:1",
    segmentId: "segment-1",
    revision: 1,
    status: "draft",
    text,
    ...overrides,
  };
}

describe("advanceLiveCaptionView", () => {
  it("coalesces Draft changes inside the 125 ms refresh window", () => {
    const initial = createLiveCaptionViewState();
    const first = advanceLiveCaptionView(initial, {
      candidate: candidate("Hello"),
      nowMs: 1_000,
    });
    const second = advanceLiveCaptionView(first.state, {
      candidate: candidate("Hello world", {
        key: "translation:segment-1:2",
        revision: 2,
      }),
      nowMs: 1_050,
    });

    expect(second.state.visible?.text).toBe("Hello");
    expect(second.state.pending?.text).toBe("Hello world");
    expect(second.nextUpdateAtMs).toBe(1_125);
  });

  it("lets Final replace the matching Draft immediately", () => {
    const draft = advanceLiveCaptionView(createLiveCaptionViewState(), {
      candidate: candidate("Hello wor"),
      nowMs: 1_000,
    });
    const final = advanceLiveCaptionView(draft.state, {
      candidate: candidate("Hello world", {
        key: "translation:segment-1:2",
        revision: 2,
        status: "final",
      }),
      nowMs: 1_050,
    });

    expect(final.state.visible?.status).toBe("final");
    expect(final.state.visible?.text).toBe("Hello world");
    expect(final.state.transition).toBe("final-replace");
    expect(final.state.finalHoldUntilMs).toBe(2_250);
  });

  it("keeps only the newest pending candidate", () => {
    const first = advanceLiveCaptionView(createLiveCaptionViewState(), {
      candidate: candidate("First", { status: "final" }),
      nowMs: 1_000,
    });
    const second = advanceLiveCaptionView(first.state, {
      candidate: candidate("Second", {
        key: "translation:segment-2:1",
        segmentId: "segment-2",
        status: "final",
      }),
      nowMs: 1_020,
    });
    const third = advanceLiveCaptionView(second.state, {
      candidate: candidate("Third", {
        key: "translation:segment-3:1",
        segmentId: "segment-3",
        status: "final",
      }),
      nowMs: 1_040,
    });
    const latest = advanceLiveCaptionView(third.state, {
      candidate: candidate("Live now", {
        key: "translation:segment-4:1",
        segmentId: "segment-4",
      }),
      nowMs: 1_050,
    });

    expect(latest.state.pending?.segmentId).toBe("segment-4");
    expect(latest.state.pending?.text).toBe("Live now");
  });

  it("promotes a pending Draft when its refresh deadline arrives", () => {
    const first = advanceLiveCaptionView(createLiveCaptionViewState(), {
      candidate: candidate("Hello"),
      nowMs: 1_000,
    });
    const pending = advanceLiveCaptionView(first.state, {
      candidate: candidate("Hello world", { revision: 2 }),
      nowMs: 1_050,
    });
    const promoted = advanceLiveCaptionView(pending.state, {
      candidate: null,
      nowMs: 1_125,
    });

    expect(promoted.state.visible?.text).toBe("Hello world");
    expect(promoted.state.pending).toBeNull();
  });

  it("ignores a stale revision for the visible segment", () => {
    const first = advanceLiveCaptionView(createLiveCaptionViewState(), {
      candidate: candidate("New", { revision: 3 }),
      nowMs: 1_000,
    });
    const stale = advanceLiveCaptionView(first.state, {
      candidate: candidate("Old", { revision: 2 }),
      nowMs: 1_200,
    });

    expect(stale.state.visible?.text).toBe("New");
    expect(stale.state.pending).toBeNull();
  });

  it("clears the visible cue when no current or pending caption remains", () => {
    const visible = advanceLiveCaptionView(createLiveCaptionViewState(), {
      candidate: candidate("Previous session"),
      nowMs: 1_000,
    });
    const cleared = advanceLiveCaptionView(visible.state, {
      candidate: null,
      nowMs: 1_500,
    });

    expect(cleared.state).toEqual(createLiveCaptionViewState());
  });
});
