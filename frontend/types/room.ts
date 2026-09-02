import type { Session } from "@/types/session";

export type ManagedRoom = {
  id: string;
  room_name: string;
  display_name: string;
  status: "ready" | "closed";
  created_at: string;
  updated_at: string;
  closed_at: string | null;
};

export type CaptionInputType = "microphone" | "screen" | "file" | "hls";

export type CaptionRun = Session & {
  room_id: string;
  source_type: CaptionInputType;
};
