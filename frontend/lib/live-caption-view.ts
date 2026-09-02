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

export type AdvanceLiveCaptionViewInput = {
  candidate: LiveCaptionCandidate | null;
  nowMs: number;
  refreshIntervalMs?: number;
  minimumFinalHoldMs?: number;
};

export type AdvanceLiveCaptionViewResult = {
  state: LiveCaptionViewState;
  nextUpdateAtMs: number | null;
};

export function createLiveCaptionViewState(): LiveCaptionViewState {
  return {
    visible: null,
    pending: null,
    lastRefreshAtMs: null,
    finalHoldUntilMs: null,
    transition: "none",
  };
}

function isSameCandidate(
  left: LiveCaptionCandidate,
  right: LiveCaptionCandidate,
): boolean {
  return left.key === right.key &&
    left.status === right.status &&
    left.text === right.text;
}

function isStaleForVisible(
  visible: LiveCaptionCandidate,
  candidate: LiveCaptionCandidate,
): boolean {
  if (visible.segmentId !== candidate.segmentId) {
    return false;
  }
  if (candidate.revision < visible.revision) {
    return true;
  }
  return candidate.revision === visible.revision &&
    visible.status === "final" &&
    candidate.status === "draft";
}

function installCandidate(
  candidate: LiveCaptionCandidate,
  nowMs: number,
  minimumFinalHoldMs: number,
  transition: LiveCaptionViewState["transition"],
): LiveCaptionViewState {
  return {
    visible: candidate,
    pending: null,
    lastRefreshAtMs: nowMs,
    finalHoldUntilMs: candidate.status === "final"
      ? nowMs + minimumFinalHoldMs
      : null,
    transition,
  };
}

export function advanceLiveCaptionView(
  previous: LiveCaptionViewState,
  input: AdvanceLiveCaptionViewInput,
): AdvanceLiveCaptionViewResult {
  const refreshIntervalMs = input.refreshIntervalMs ?? 125;
  const minimumFinalHoldMs = input.minimumFinalHoldMs ?? 1_200;
  if (!Number.isFinite(input.nowMs)) {
    throw new Error("nowMs must be finite");
  }
  if (!Number.isFinite(refreshIntervalMs) || refreshIntervalMs <= 0) {
    throw new Error("refreshIntervalMs must be positive");
  }
  if (!Number.isFinite(minimumFinalHoldMs) || minimumFinalHoldMs < 0) {
    throw new Error("minimumFinalHoldMs must not be negative");
  }

  const refreshAt = previous.lastRefreshAtMs === null
    ? input.nowMs
    : previous.lastRefreshAtMs + refreshIntervalMs;
  const duePending = previous.pending !== null && input.nowMs >= refreshAt
    ? previous.pending
    : null;
  const candidate = input.candidate ?? duePending;

  if (candidate === null) {
    if (previous.pending === null) {
      return {
        state: createLiveCaptionViewState(),
        nextUpdateAtMs: null,
      };
    }
    return {
      state: { ...previous, transition: "none" },
      nextUpdateAtMs: refreshAt,
    };
  }

  if (previous.visible === null) {
    return {
      state: installCandidate(
        candidate,
        input.nowMs,
        minimumFinalHoldMs,
        "none",
      ),
      nextUpdateAtMs: null,
    };
  }

  if (isSameCandidate(previous.visible, candidate)) {
    return {
      state: { ...previous, pending: null, transition: "none" },
      nextUpdateAtMs: null,
    };
  }

  if (isStaleForVisible(previous.visible, candidate)) {
    return {
      state: { ...previous, transition: "none" },
      nextUpdateAtMs: previous.pending === null ? null : refreshAt,
    };
  }

  const matchingFinal =
    candidate.segmentId === previous.visible.segmentId &&
    candidate.status === "final" &&
    candidate.revision >= previous.visible.revision;
  if (matchingFinal) {
    return {
      state: installCandidate(
        candidate,
        input.nowMs,
        minimumFinalHoldMs,
        "final-replace",
      ),
      nextUpdateAtMs: null,
    };
  }

  if (input.nowMs < refreshAt) {
    return {
      state: { ...previous, pending: candidate, transition: "none" },
      nextUpdateAtMs: refreshAt,
    };
  }

  const heldFinal =
    previous.visible.status === "final" &&
    candidate.status === "final" &&
    previous.finalHoldUntilMs !== null &&
    input.nowMs < previous.finalHoldUntilMs;
  if (heldFinal) {
    return {
      state: { ...previous, pending: candidate, transition: "none" },
      nextUpdateAtMs: previous.finalHoldUntilMs,
    };
  }

  return {
    state: installCandidate(
      candidate,
      input.nowMs,
      minimumFinalHoldMs,
      "none",
    ),
    nextUpdateAtMs: null,
  };
}
