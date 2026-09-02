export type StudioSidebarTab = "rooms" | "assistants";
export type StudioSidebarState = { tab: StudioSidebarTab; collapsed: boolean; width: number };
export type StudioSidebarAction =
  | { type: "toggle" | "open"; tab: StudioSidebarTab }
  | { type: "collapse" }
  | { type: "resize"; width: number };

export const SIDEBAR_MIN_WIDTH = 320;
export const SIDEBAR_MAX_WIDTH = 560;
export const SIDEBAR_RAIL_WIDTH = 56;

export function clampSidebarWidth(width: number): number {
  return Number.isFinite(width)
    ? Math.max(SIDEBAR_MIN_WIDTH, Math.min(SIDEBAR_MAX_WIDTH, Math.round(width)))
    : 384;
}

export function createStudioSidebarState(): StudioSidebarState {
  return { tab: "rooms", collapsed: false, width: 384 };
}

export function reduceStudioSidebar(state: StudioSidebarState, action: StudioSidebarAction): StudioSidebarState {
  switch (action.type) {
    case "toggle":
      return { ...state, tab: action.tab, collapsed: state.tab === action.tab && !state.collapsed };
    case "open":
      return { ...state, tab: action.tab, collapsed: false };
    case "collapse":
      return { ...state, collapsed: true };
    case "resize":
      return { ...state, width: clampSidebarWidth(action.width) };
  }
}
