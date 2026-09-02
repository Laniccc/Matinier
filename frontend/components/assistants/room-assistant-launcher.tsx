"use client";

export function RoomAssistantLauncher({ onOpen, expanded }: { onOpen: () => void; expanded: boolean }) {
  return (
    <button type="button" className="studioAssistantLink" aria-controls="studio-panel-assistants"
      aria-expanded={expanded} onClick={onOpen}>
      助手工作区
    </button>
  );
}
