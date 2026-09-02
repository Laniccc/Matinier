export type SessionStatus =
  | "created"
  | "starting"
  | "running"
  | "finalizing"
  | "completed"
  | "failed"
  | "cancelled";

export type Session = {
  id: string;
  room_id: string | null;
  room_name: string;
  status: SessionStatus;
  source_type: "empty" | "file" | "microphone" | "screen" | "hls";
  source_name: string;
  language: string;
  target_language: string | null;
  asr_provider: string | null;
  asr_model: string | null;
  translation_status:
    | "disabled"
    | "starting"
    | "running"
    | "completed"
    | "failed";
  translation_provider: string | null;
  translation_model: string | null;
  translation_error_code: string | null;
  translation_error_message: string | null;
  final_result_count: number | null;
  first_partial_latency_ms: number | null;
  average_final_latency_ms: number | null;
  provider_error_count: number | null;
  sent_audio_chunk_count: number | null;
  sent_audio_bytes: number | null;
  error_code: string | null;
  error_message: string | null;
  stop_reason: string | null;
  failure_code: string | null;
  failure_detail: string | null;
  started_at: string | null;
  ended_at: string | null;
  source_ended_at: string | null;
  translation_ended_at: string | null;
  created_at: string;
};

export type SessionRuntime = {
  session_status: SessionStatus;
  source_status: "starting" | "running" | "stopping" | "stopped" | "failed" | "lost";
  translation_status: "disabled" | "starting" | "running" | "completed" | "failed";
  cleanup_status: string;
  room_connected: boolean | null;
  publisher_connected: boolean | null;
  track_published: boolean | null;
  track_subscribed: boolean | null;
  asr_connected: boolean | null;
  translation_connected: boolean | null;
  ffmpeg_running: boolean | null;
  audio_bytes: number | null;
  audio_frames: number | null;
  audio_queue_current: number | null;
  audio_queue_max: number | null;
  source_final_count: number;
  translation_final_count: number;
  last_event_at: string | null;
  started_at: string | null;
  ended_at: string | null;
  source_ended_at: string | null;
  translation_ended_at: string | null;
  failure_code: string | null;
  stop_reason: string | null;
  unavailable: string[];
};

export type Segment = {
  id: string;
  session_id: string;
  segment_id: string;
  track_id: string;
  revision: number;
  language: string;
  raw_text: string;
  display_text: string;
  audio_start_ms: number | null;
  audio_end_ms: number | null;
  confidence: number | null;
  status: "final";
  received_at_ms: number;
  finalized_at: string;
  created_at: string;
  updated_at: string;
};

export type TranslationSegment = {
  id: string;
  session_id: string;
  segment_id: string;
  revision: number;
  source_language: string;
  target_language: string;
  text: string;
  audio_start_ms: number | null;
  audio_end_ms: number | null;
  source_segment_ids: string[];
  status: "final";
  received_at_ms: number;
  finalized_at: string;
  created_at: string;
  updated_at: string;
};

export type LiveKitToken = {
  token: string;
  url: string;
  room_name: string;
  participant_identity: string;
};

export type ExportFormat = "json" | "srt" | "vtt" | "markdown";
