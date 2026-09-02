export type PackageStatus = "building" | "frozen" | "superseded" | "invalid";

export type PackageDocumentReference = {
  document_id: string;
  document_kind: string;
  language: string | null;
  content_hash: string;
};

export type PackageManifest = {
  schema: "matinier.transcript-package";
  schema_version: "1.0";
  package_id: string;
  package_version: number;
  session_id: string;
  status: "frozen";
  source_language: string;
  target_languages: string[];
  effective_source_document_id: string;
  source_revision_id: string | null;
  created_at: string;
  content_hash: string;
  documents: PackageDocumentReference[];
  session_snapshot: Record<string, unknown>;
  provider_snapshot: Record<string, unknown>;
  metrics_snapshot: Record<string, unknown>;
};

export type PackageTranscriptItem = {
  item_id: string;
  source_segment_ids: string[];
  start_ms: number;
  end_ms: number;
  text: string;
  raw_text: string | null;
  confidence: number | null;
  speaker: string | null;
  locked: boolean;
};

export type PackageDocument = {
  document_id: string;
  document_kind: string;
  language: string | null;
  content: {
    items?: PackageTranscriptItem[];
    duration_ms?: number;
    [key: string]: unknown;
  };
  content_hash: string;
};

export type PackageSummary = {
  package_id: string;
  session_id: string;
  package_version: number;
  schema_name: string;
  schema_version: string;
  status: PackageStatus;
  content_hash: string | null;
  source_revision_id: string | null;
  created_at: string;
  frozen_at: string | null;
  superseded_at: string | null;
};

export type TranscriptPackage = PackageSummary & {
  manifest: PackageManifest;
  documents: PackageDocument[];
};

export type PackageValidation = {
  valid: boolean;
  package_id: string;
  content_hash: string | null;
  errors: string[];
};
