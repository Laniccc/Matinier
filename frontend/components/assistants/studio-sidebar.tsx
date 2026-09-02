"use client";

import { useRef, type ReactNode, type KeyboardEvent } from "react";
import {
  SIDEBAR_MIN_WIDTH,
  SIDEBAR_MAX_WIDTH,
  type StudioSidebarAction,
  type StudioSidebarState,
  type StudioSidebarTab,
} from "@/lib/studio-sidebar-state";

export function StudioSidebar({ state, onAction, rooms, assistants }: {
  state: StudioSidebarState;
  onAction: (action: StudioSidebarAction) => void;
  rooms: ReactNode;
  assistants: ReactNode;
}) {
  const buttons = useRef<Partial<Record<StudioSidebarTab, HTMLButtonElement | null>>>({});
  const drag = useRef<{ pointerId: number; x: number; width: number } | null>(null);
  const collapse = () => {
    onAction({ type: "collapse" });
    buttons.current[state.tab]?.focus();
  };
  const resizeWithKeys = (event: KeyboardEvent<HTMLDivElement>) => {
    const width = event.key === "ArrowRight" ? state.width + 24
      : event.key === "ArrowLeft" ? state.width - 24
      : event.key === "Home" ? SIDEBAR_MIN_WIDTH
      : event.key === "End" ? SIDEBAR_MAX_WIDTH : null;
    if (width === null) return;
    event.preventDefault();
    onAction({ type: "resize", width });
  };

  return (
    <aside className={`studioSidebar${state.collapsed ? " isCollapsed" : ""}`} aria-label="直播工作区侧栏"
      onKeyDown={(event) => {
        if (event.key === "Escape" && !state.collapsed) {
          event.preventDefault();
          collapse();
        }
      }}>
      <nav className="studioActivityBar" aria-label="工作区面板">
        {(["rooms", "assistants"] as const).map((tab) => (
          <button key={tab} type="button"
            ref={(element) => { buttons.current[tab] = element; }}
            id={`studio-activity-${tab}`}
            aria-label={tab === "rooms" ? "直播空间" : "助手"}
            title={tab === "rooms" ? "直播空间" : "助手工作区"}
            aria-controls={`studio-panel-${tab}`}
            aria-expanded={state.tab === tab && !state.collapsed}
            className={state.tab === tab && !state.collapsed ? "active" : ""}
            onClick={() => onAction({ type: "toggle", tab })}>
            <svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.6">
              {tab === "rooms" ? <><rect x="3" y="4" width="18" height="13" rx="2" /><path d="M8 21h8M12 17v4m-3-9 6 3-6 3z" /></>
                : <><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" /><path d="M17.5 13v9M13 17.5h9" /></>}
            </svg>
            <span>{tab === "rooms" ? "空间" : "助手"}</span>
          </button>
        ))}
      </nav>
      <div className="studioSidebarBody" hidden={state.collapsed}>
        <header className="studioSidebarHeading">
          <strong>{state.tab === "rooms" ? "直播空间" : "助手工作区"}</strong>
          <button type="button" className="iconButton" aria-label="收起侧栏" title="收起侧栏 (Esc)" onClick={collapse}>‹</button>
        </header>
        {/* Hide panels rather than unmounting: plugin connection and form state stay alive. */}
        <div id="studio-panel-rooms" role="region" aria-labelledby="studio-activity-rooms"
          className="studioSidebarPanel" hidden={state.collapsed || state.tab !== "rooms"}>{rooms}</div>
        <div id="studio-panel-assistants" role="region" aria-labelledby="studio-activity-assistants"
          className="studioSidebarPanel" hidden={state.collapsed || state.tab !== "assistants"}>{assistants}</div>
      </div>
      <div className="studioSidebarResize" hidden={state.collapsed} role="separator" tabIndex={state.collapsed ? -1 : 0}
        aria-label="调整侧栏宽度" aria-orientation="vertical" aria-controls={`studio-panel-${state.tab}`}
        aria-valuemin={SIDEBAR_MIN_WIDTH} aria-valuemax={SIDEBAR_MAX_WIDTH} aria-valuenow={state.width}
        aria-valuetext={`${state.width} 像素`} title="拖动调整宽度，或使用左右方向键"
        onKeyDown={resizeWithKeys}
        onPointerDown={(event) => {
          if (event.button !== 0) return;
          event.preventDefault();
          drag.current = { pointerId: event.pointerId, x: event.clientX, width: state.width };
          event.currentTarget.setPointerCapture(event.pointerId);
          event.currentTarget.focus();
        }}
        onPointerMove={(event) => {
          const active = drag.current;
          if (active?.pointerId === event.pointerId) {
            onAction({ type: "resize", width: active.width + event.clientX - active.x });
          }
        }}
        onPointerUp={(event) => {
          if (drag.current?.pointerId !== event.pointerId) return;
          drag.current = null;
          event.currentTarget.releasePointerCapture(event.pointerId);
        }}
        onPointerCancel={() => { drag.current = null; }}
        onLostPointerCapture={() => { drag.current = null; }} />
    </aside>
  );
}
