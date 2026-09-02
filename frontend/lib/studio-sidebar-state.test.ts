import { describe, expect, it } from "vitest";
import { clampSidebarWidth, createStudioSidebarState, reduceStudioSidebar } from "./studio-sidebar-state";

describe("studio sidebar UI state", () => {
  it("starts on rooms with a usable expanded width", () => {
    expect(createStudioSidebarState()).toEqual({ tab: "rooms", collapsed: false, width: 384 });
  });
  it("collapses the active tab and opens the other tab without changing width", () => {
    const folded = reduceStudioSidebar(createStudioSidebarState(), { type: "toggle", tab: "rooms" });
    expect(folded.collapsed).toBe(true);
    expect(reduceStudioSidebar(folded, { type: "toggle", tab: "assistants" })).toEqual({
      tab: "assistants", collapsed: false, width: 384,
    });
  });
  it("explicit open never toggles a visible assistant off", () => {
    const action = { type: "open", tab: "assistants" } as const;
    const opened = reduceStudioSidebar(createStudioSidebarState(), action);
    expect(reduceStudioSidebar(opened, action)).toEqual(opened);
  });
  it("retains selection/width when collapsing and clamps resize inputs", () => {
    let state = reduceStudioSidebar(createStudioSidebarState(), { type: "resize", width: 500 });
    state = reduceStudioSidebar(state, { type: "collapse" });
    expect(state).toEqual({ tab: "rooms", collapsed: true, width: 500 });
    expect(clampSidebarWidth(100)).toBe(320);
    expect(clampSidebarWidth(9999)).toBe(560);
    expect(clampSidebarWidth(Number.NaN)).toBe(384);
    expect(clampSidebarWidth(Infinity)).toBe(384);
    expect(clampSidebarWidth(400.6)).toBe(401);
  });
});
