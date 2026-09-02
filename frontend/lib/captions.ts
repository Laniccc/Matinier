import type {
  CaptionPayload,
  CaptionState,
  LiveCaptionEvent,
  SessionMetrics,
  TranslationPayload,
  TranslationStatus,
} from "@/types/captions";
import type {
  Segment,
  SessionStatus,
  TranslationSegment,
} from "@/types/session";

type UnknownRecord = Record<string, unknown>;

const ENVELOPE_KEYS = [
  "schema_version",
  "topic",
  "type",
  "session_id",
  "sent_at_ms",
  "payload",
] as const;

const CAPTION_KEYS = [
  "segment_id",
  "revision",
  "status",
  "text",
  "audio_start_ms",
  "audio_end_ms",
  "confidence",
  "provider_event_id",
  "received_at_ms",
] as const;

const METRICS_KEYS = [
  "final_result_count",
  "first_partial_latency_ms",
  "average_final_latency_ms",
  "provider_error_count",
  "sent_audio_chunk_count",
  "sent_audio_bytes",
] as const;

const TRANSLATION_KEYS = [
  "segment_id",
  "revision",
  "status",
  "text",
  "source_language",
  "target_language",
  "audio_start_ms",
  "audio_end_ms",
  "source_segment_ids",
  "provider_event_id",
  "received_at_ms",
] as const;

const TRANSLATION_STATUS_KEYS = [
  "status",
  "source_language",
  "target_language",
  "error_code",
  "message",
] as const;

function isRecord(value: unknown): value is UnknownRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys(value: UnknownRecord, expected: readonly string[]): boolean {
  const actual = Object.keys(value);
  return (
    actual.length === expected.length &&
    expected.every((key) => Object.hasOwn(value, key))
  );
}

function isNonNegativeInteger(value: unknown): value is number {
  return Number.isInteger(value) && Number(value) >= 0;
}

function isPositiveInteger(value: unknown): value is number {
  return Number.isInteger(value) && Number(value) > 0;
}

function isNonNegativeNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function isNullableInteger(value: unknown): value is number | null {
  return value === null || isNonNegativeInteger(value);
}

function isNullableNumber(value: unknown): value is number | null {
  return value === null || isNonNegativeNumber(value);
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function isCaptionPayload(value: unknown): value is CaptionPayload {
  if (!isRecord(value) || !hasExactKeys(value, CAPTION_KEYS)) {
    return false;
  }
  return (
    typeof value.segment_id === "string" &&
    value.segment_id.length > 0 &&
    isPositiveInteger(value.revision) &&
    (value.status === "draft" || value.status === "final") &&
    typeof value.text === "string" &&
    value.text.trim().length > 0 &&
    isNullableInteger(value.audio_start_ms) &&
    isNullableInteger(value.audio_end_ms) &&
    isNullableNumber(value.confidence) &&
    typeof value.provider_event_id === "string" &&
    value.provider_event_id.length > 0 &&
    isNonNegativeInteger(value.received_at_ms)
  );
}

function isSessionMetrics(value: unknown): value is SessionMetrics {
  if (!isRecord(value) || !hasExactKeys(value, METRICS_KEYS)) {
    return false;
  }
  return (
    isNonNegativeInteger(value.final_result_count) &&
    isNullableNumber(value.first_partial_latency_ms) &&
    isNullableNumber(value.average_final_latency_ms) &&
    isNonNegativeInteger(value.provider_error_count) &&
    isNonNegativeInteger(value.sent_audio_chunk_count) &&
    isNonNegativeInteger(value.sent_audio_bytes)
  );
}

function isTranslationPayload(value: unknown): value is TranslationPayload {
  if (!isRecord(value) || !hasExactKeys(value, TRANSLATION_KEYS)) {
    return false;
  }
  return (
    typeof value.segment_id === "string" &&
    value.segment_id.length > 0 &&
    isPositiveInteger(value.revision) &&
    (value.status === "draft" || value.status === "final") &&
    typeof value.text === "string" &&
    value.text.trim().length > 0 &&
    typeof value.source_language === "string" &&
    value.source_language.length >= 2 &&
    typeof value.target_language === "string" &&
    value.target_language.length >= 2 &&
    isNullableInteger(value.audio_start_ms) &&
    isNullableInteger(value.audio_end_ms) &&
    isStringArray(value.source_segment_ids) &&
    typeof value.provider_event_id === "string" &&
    value.provider_event_id.length > 0 &&
    isNonNegativeInteger(value.received_at_ms)
  );
}

export function decodeLiveCaptionEvent(
  encoded: Uint8Array | string,
  activeSessionId: string,
): LiveCaptionEvent | null {
  let parsed: unknown;
  try {
    const json =
      typeof encoded === "string"
        ? encoded
        : new TextDecoder("utf-8", { fatal: true }).decode(encoded);
    parsed = JSON.parse(json) as unknown;
  } catch {
    return null;
  }

  if (
    !isRecord(parsed) ||
    !hasExactKeys(parsed, ENVELOPE_KEYS) ||
    parsed.schema_version !== 1 ||
    parsed.session_id !== activeSessionId ||
    !isNonNegativeInteger(parsed.sent_at_ms)
  ) {
    return null;
  }

  if (
    parsed.topic === "caption" &&
    parsed.type === "caption.upsert" &&
    isCaptionPayload(parsed.payload)
  ) {
    return parsed as LiveCaptionEvent;
  }

  if (
    parsed.topic === "translation" &&
    parsed.type === "translation.upsert" &&
    isTranslationPayload(parsed.payload)
  ) {
    return parsed as LiveCaptionEvent;
  }
  if (
    parsed.topic === "translation" &&
    parsed.type === "translation.status" &&
    isRecord(parsed.payload) &&
    hasExactKeys(parsed.payload, TRANSLATION_STATUS_KEYS) &&
    [
      "starting",
      "running",
      "completed",
      "failed",
    ].includes(String(parsed.payload.status)) &&
    typeof parsed.payload.source_language === "string" &&
    typeof parsed.payload.target_language === "string" &&
    (parsed.payload.error_code === null ||
      typeof parsed.payload.error_code === "string") &&
    (parsed.payload.message === null ||
      typeof parsed.payload.message === "string")
  ) {
    return parsed as LiveCaptionEvent;
  }

  if (parsed.topic !== "session" || !isRecord(parsed.payload)) {
    return null;
  }
  if (
    parsed.type === "session.status" &&
    hasExactKeys(parsed.payload, ["status"]) &&
    [
      "running",
      "finalizing",
      "completed",
      "failed",
      "cancelled",
    ].includes(
      String(parsed.payload.status),
    )
  ) {
    return parsed as LiveCaptionEvent;
  }
  if (
    parsed.type === "session.progress" &&
    hasExactKeys(parsed.payload, ["audio_time_ms"]) &&
    isNonNegativeInteger(parsed.payload.audio_time_ms)
  ) {
    return parsed as LiveCaptionEvent;
  }
  if (
    parsed.type === "session.metrics" &&
    isSessionMetrics(parsed.payload)
  ) {
    return parsed as LiveCaptionEvent;
  }
  if (
    parsed.type === "session.error" &&
    hasExactKeys(parsed.payload, ["error_code", "message"]) &&
    typeof parsed.payload.error_code === "string" &&
    parsed.payload.error_code.length > 0 &&
    typeof parsed.payload.message === "string" &&
    parsed.payload.message.length > 0
  ) {
    return parsed as LiveCaptionEvent;
  }
  return null;
}

export function createCaptionState(
  sessionStatus: SessionStatus = "created",
  translationStatus: TranslationStatus = "disabled",
): CaptionState {
  return {
    sessionStatus,
    activeDraftSegments: {},
    finalSegments: {},
    lastRevisionBySegment: {},
    translationStatus,
    translationDraftSegments: {},
    translationFinalSegments: {},
    lastTranslationRevisionBySegment: {},
    translationError: null,
    error: null,
    metricsSummary: null,
    currentAudioTimeMs: 0,
  };
}

function mergeTranslation(
  state: CaptionState,
  translation: TranslationPayload,
): CaptionState {
  const currentRevision =
    state.lastTranslationRevisionBySegment[translation.segment_id] ?? 0;
  if (translation.revision <= currentRevision) {
    return state;
  }
  if (
    translation.status === "draft" &&
    Object.hasOwn(
      state.translationFinalSegments,
      translation.segment_id,
    )
  ) {
    return state;
  }
  const lastTranslationRevisionBySegment = {
    ...state.lastTranslationRevisionBySegment,
    [translation.segment_id]: translation.revision,
  };
  if (translation.status === "draft") {
    return {
      ...state,
      lastTranslationRevisionBySegment,
      translationDraftSegments: {
        ...state.translationDraftSegments,
        [translation.segment_id]: translation,
      },
    };
  }
  const translationDraftSegments = {
    ...state.translationDraftSegments,
  };
  delete translationDraftSegments[translation.segment_id];
  return {
    ...state,
    lastTranslationRevisionBySegment,
    translationDraftSegments,
    translationFinalSegments: {
      ...state.translationFinalSegments,
      [translation.segment_id]: translation,
    },
  };
}

function mergeCaption(
  state: CaptionState,
  caption: CaptionPayload,
): CaptionState {
  const currentRevision = state.lastRevisionBySegment[caption.segment_id] ?? 0;
  if (caption.revision <= currentRevision) {
    return state;
  }
  if (
    caption.status === "draft" &&
    Object.hasOwn(state.finalSegments, caption.segment_id)
  ) {
    return state;
  }

  const lastRevisionBySegment = {
    ...state.lastRevisionBySegment,
    [caption.segment_id]: caption.revision,
  };
  if (caption.status === "draft") {
    return {
      ...state,
      lastRevisionBySegment,
      activeDraftSegments: {
        ...state.activeDraftSegments,
        [caption.segment_id]: caption,
      },
    };
  }

  const activeDraftSegments = { ...state.activeDraftSegments };
  delete activeDraftSegments[caption.segment_id];
  return {
    ...state,
    lastRevisionBySegment,
    activeDraftSegments,
    finalSegments: {
      ...state.finalSegments,
      [caption.segment_id]: caption,
    },
  };
}

export function reduceCaptionEvent(
  state: CaptionState,
  event: LiveCaptionEvent,
): CaptionState {
  switch (event.type) {
    case "caption.upsert":
      return mergeCaption(state, event.payload);
    case "session.status":
      return { ...state, sessionStatus: event.payload.status };
    case "session.progress":
      return { ...state, currentAudioTimeMs: event.payload.audio_time_ms };
    case "session.metrics":
      return { ...state, metricsSummary: event.payload };
    case "session.error":
      return { ...state, error: event.payload.message };
    case "translation.upsert":
      return mergeTranslation(state, event.payload);
    case "translation.status":
      return {
        ...state,
        translationStatus: event.payload.status,
        translationError:
          event.payload.status === "failed"
            ? event.payload.message
            : null,
      };
  }
}

export function hydrateFinalSnapshot(
  state: CaptionState,
  snapshot: readonly Segment[],
): CaptionState {
  return snapshot.reduce((current, segment) => {
    if (segment.status !== "final" || !segment.display_text.trim()) {
      return current;
    }
    const receivedAt = Date.parse(segment.finalized_at);
    return mergeCaption(current, {
      segment_id: segment.segment_id,
      revision: segment.revision,
      status: "final",
      text: segment.display_text,
      audio_start_ms: segment.audio_start_ms,
      audio_end_ms: segment.audio_end_ms,
      confidence: segment.confidence,
      provider_event_id: `snapshot:${segment.id}`,
      received_at_ms: Number.isFinite(receivedAt) ? receivedAt : 0,
    });
  }, state);
}

export function hydrateTranslationSnapshot(
  state: CaptionState,
  snapshot: readonly TranslationSegment[],
): CaptionState {
  return snapshot.reduce((current, segment) => {
    if (!segment.text.trim()) {
      return current;
    }
    const receivedAt = Date.parse(segment.finalized_at);
    return mergeTranslation(current, {
      segment_id: segment.segment_id,
      revision: segment.revision,
      status: "final",
      text: segment.text,
      source_language: segment.source_language,
      target_language: segment.target_language,
      audio_start_ms: segment.audio_start_ms,
      audio_end_ms: segment.audio_end_ms,
      source_segment_ids: segment.source_segment_ids,
      provider_event_id: `snapshot:${segment.id}`,
      received_at_ms: Number.isFinite(receivedAt) ? receivedAt : 0,
    });
  }, state);
}

function timeKey(value: number | null): number {
  return value === null ? Number.POSITIVE_INFINITY : value;
}

function ordered(values: Record<string, CaptionPayload>): CaptionPayload[] {
  return Object.values(values).sort(
    (left, right) =>
      timeKey(left.audio_start_ms) - timeKey(right.audio_start_ms) ||
      timeKey(left.audio_end_ms) - timeKey(right.audio_end_ms) ||
      left.segment_id.localeCompare(right.segment_id),
  );
}

function orderedTranslations(
  values: Record<string, TranslationPayload>,
): TranslationPayload[] {
  return Object.values(values).sort(
    (left, right) =>
      timeKey(left.audio_start_ms) - timeKey(right.audio_start_ms) ||
      timeKey(left.audio_end_ms) - timeKey(right.audio_end_ms) ||
      left.segment_id.localeCompare(right.segment_id),
  );
}

export function selectActiveDraftSegments(state: CaptionState): CaptionPayload[] {
  return ordered(state.activeDraftSegments);
}

export function selectFinalSegments(state: CaptionState): CaptionPayload[] {
  return ordered(state.finalSegments).filter(
    (caption) => caption.text.trim().length > 0,
  );
}

export function selectTranslationDraftSegments(
  state: CaptionState,
): TranslationPayload[] {
  return orderedTranslations(state.translationDraftSegments);
}

export function selectTranslationFinalSegments(
  state: CaptionState,
): TranslationPayload[] {
  return orderedTranslations(state.translationFinalSegments).filter(
    (caption) => caption.text.trim().length > 0,
  );
}
