"use client";

import type { Session } from "@/types/session";


const STATUS_LABELS: Record<Session["status"], string> = {
  created: "等待输入",
  starting: "正在启动",
  running: "进行中",
  finalizing: "正在收尾",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

function sessionLabel(session: Session): string {
  return session.room_name || session.source_name || session.id;
}

export function AssistantSessionSelector({
  sessions,
  selectedSessionId,
  onSelect,
}: {
  sessions: Session[];
  selectedSessionId: string | null;
  onSelect: (sessionId: string) => void;
}) {
  return (
    <label className="assistantSessionSelector">
      <span>字幕 Session</span>
      <select
        value={selectedSessionId ?? ""}
        onChange={(event) => onSelect(event.target.value)}
        disabled={sessions.length === 0}
      >
        {sessions.length === 0 ? <option value="">暂无 Session</option> : null}
        {sessions.map((session) => (
          <option key={session.id} value={session.id}>
            {sessionLabel(session)} · {STATUS_LABELS[session.status]}
          </option>
        ))}
      </select>
    </label>
  );
}
