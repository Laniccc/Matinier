export type RevisionStatus = "saved" | "approved" | "superseded";

export type RevisionItem = {
  item_id: string;
  source_segment_ids: string[];
  start_ms: number;
  end_ms: number;
  text: string;
};

export type TranscriptRevisionContent = {
  items: RevisionItem[];
};

export type RevisionSummary = {
  revision_id: string;
  session_id: string;
  version: number;
  parent_revision_id: string | null;
  base_package_id: string;
  language: string;
  content_hash: string;
  change_summary: string | null;
  status: RevisionStatus;
  item_count: number;
  created_at: string;
  approved_at: string | null;
};

export type TranscriptRevision = Omit<RevisionSummary, "item_count"> & {
  content: TranscriptRevisionContent;
};

export type RevisionExportFormat = "srt" | "vtt" | "markdown";
