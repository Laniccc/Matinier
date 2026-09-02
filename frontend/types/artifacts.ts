export type ArtifactKind =
  | "clean_script"
  | "refined_translation"
  | "summary"
  | "chapter_outline"
  | "timeline_fact_review";

export type ProcessingJobStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

export type ProcessingJob = {
  job_id: string;
  package_id: string;
  target_artifact_id: string | null;
  result_artifact_id: string | null;
  artifact_kind: ArtifactKind;
  status: ProcessingJobStatus;
  progress: number;
  provider: string | null;
  model: string | null;
  options: Record<string, unknown>;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  started_at: string | null;
  ended_at: string | null;
};

export type ArtifactEvidence = {
  evidence_key: string;
  source_item_ids: string[];
  source_segment_ids: string[];
  start_ms: number;
  end_ms: number;
};

export type CleanScriptSection = {
  source_item_ids: string[];
  source_segment_ids: string[];
  start_ms: number;
  end_ms: number;
  clean_text: string;
  notes: string[];
};

export type RefinedTranslationSection = {
  source_item_ids: string[];
  source_segment_ids: string[];
  start_ms: number;
  end_ms: number;
  source_text: string;
  translated_text: string;
  notes: string[];
};

export type ArtifactSection = {
  source_item_ids: string[];
  source_segment_ids: string[];
  start_ms: number;
  end_ms: number;
  clean_text?: string;
  source_text?: string;
  translated_text?: string;
  notes: string[];
};

export type EvidenceTimeRange = {
  item_id: string;
  start_ms: number;
  end_ms: number;
};

export type EvidenceExcerpt = EvidenceTimeRange & {
  text: string;
  source_segment_ids: string[];
};

export type SummaryKeyPoint = {
  text: string;
  evidence_item_ids: string[];
  source_segment_ids: string[];
  start_ms: number;
  end_ms: number;
  time_ranges: EvidenceTimeRange[];
  evidence_excerpts: EvidenceExcerpt[];
};

export type ChapterOutlineChapter = SummaryKeyPoint & {
  title: string;
  summary: string;
};

export type FactReviewStatus =
  | "supported"
  | "partially_supported"
  | "contradicted"
  | "unsupported"
  | "ambiguous";

export type FactReviewEntry = {
  claim_id: string;
  target_path: string;
  claim_text: string;
  status: FactReviewStatus;
  explanation: string | null;
  evidence_item_ids: string[];
  source_segment_ids: string[];
  start_ms: number | null;
  end_ms: number | null;
  time_ranges: EvidenceTimeRange[];
  evidence_excerpts: EvidenceExcerpt[];
};

export type FactReviewTarget = {
  artifact_id: string;
  artifact_kind: ArtifactKind;
  artifact_version: number;
  package_id: string;
  package_content_hash: string;
};

export type ArtifactExportFormat = "json" | "srt" | "vtt" | "markdown";

export type ArtifactContent = {
  title?: string;
  target_language?: string;
  sections?: ArtifactSection[];
  brief?: string;
  key_points?: SummaryKeyPoint[];
  chapters?: ChapterOutlineChapter[];
  reviews?: FactReviewEntry[];
  target_artifact?: FactReviewTarget;
  warnings?: string[];
  [key: string]: unknown;
};

export type DerivedArtifact = {
  artifact_id: string;
  package_id: string;
  package_version: number;
  package_content_hash: string;
  artifact_kind: ArtifactKind;
  identity_key: string;
  artifact_version: number;
  target_language: string | null;
  status: "generated" | "reviewed" | "approved" | "superseded" | "failed";
  provider: string | null;
  model: string | null;
  workflow_version: string;
  options: Record<string, unknown>;
  content: ArtifactContent;
  evidence: ArtifactEvidence[];
  parent_artifact_id: string | null;
  created_by: "model" | "human";
  created_at: string;
  approved_at: string | null;
};
