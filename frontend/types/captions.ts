import type { SessionStatus } from "@/types/session";

export const LIVE_CAPTION_TOPIC = "livecaption.events.v1" as const;

export type CaptionStatus = "draft" | "final";

export type CaptionPayload = {
  segment_id: string;
  revision: number;
  status: CaptionStatus;
  text: string;
  audio_start_ms: number | null;
  audio_end_ms: number | null;
  confidence: number | null;
  provider_event_id: string;
  received_at_ms: number;
};

export type TranslationStatus =
  | "disabled"
  | "starting"
  | "running"
  | "completed"
  | "failed";

export type TranslationPayload = {
  segment_id: string;
  revision: number;
  status: CaptionStatus;
  text: string;
  source_language: string;
  target_language: string;
  audio_start_ms: number | null;
  audio_end_ms: number | null;
  source_segment_ids: string[];
  provider_event_id: string;
  received_at_ms: number;
};

export type SessionMetrics = {
  final_result_count: number;
  first_partial_latency_ms: number | null;
  average_final_latency_ms: number | null;
  provider_error_count: number;
  sent_audio_chunk_count: number;
  sent_audio_bytes: number;
};

type LiveEventEnvelope<
  Topic extends "caption" | "translation" | "session",
  Type extends string,
  Payload,
> = {
  schema_version: 1;
  topic: Topic;
  type: Type;
  session_id: string;
  sent_at_ms: number;
  payload: Payload;
};

export type CaptionUpsertEvent = LiveEventEnvelope<
  "caption",
  "caption.upsert",
  CaptionPayload
>;

export type SessionStatusEvent = LiveEventEnvelope<
  "session",
  "session.status",
  { status: Exclude<SessionStatus, "created" | "starting"> }
>;

export type SessionProgressEvent = LiveEventEnvelope<
  "session",
  "session.progress",
  { audio_time_ms: number }
>;

export type SessionMetricsEvent = LiveEventEnvelope<
  "session",
  "session.metrics",
  SessionMetrics
>;

export type SessionErrorEvent = LiveEventEnvelope<
  "session",
  "session.error",
  { error_code: string; message: string }
>;

export type TranslationUpsertEvent = LiveEventEnvelope<
  "translation",
  "translation.upsert",
  TranslationPayload
>;

export type TranslationStatusEvent = LiveEventEnvelope<
  "translation",
  "translation.status",
  {
    status: Exclude<TranslationStatus, "disabled">;
    source_language: string;
    target_language: string;
    error_code: string | null;
    message: string | null;
  }
>;

export type LiveCaptionEvent =
  | CaptionUpsertEvent
  | SessionStatusEvent
  | SessionProgressEvent
  | SessionMetricsEvent
  | SessionErrorEvent
  | TranslationUpsertEvent
  | TranslationStatusEvent;

export type CaptionState = {
  sessionStatus: SessionStatus;
  activeDraftSegments: Record<string, CaptionPayload>;
  finalSegments: Record<string, CaptionPayload>;
  lastRevisionBySegment: Record<string, number>;
  translationStatus: TranslationStatus;
  translationDraftSegments: Record<string, TranslationPayload>;
  translationFinalSegments: Record<string, TranslationPayload>;
  lastTranslationRevisionBySegment: Record<string, number>;
  translationError: string | null;
  error: string | null;
  metricsSummary: SessionMetrics | null;
  currentAudioTimeMs: number;
};
