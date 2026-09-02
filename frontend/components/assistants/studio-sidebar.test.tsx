// @vitest-environment jsdom
import { act, useEffect, useReducer } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createStudioSidebarState, reduceStudioSidebar } from "@/lib/studio-sidebar-state";
import { StudioSidebar } from "./studio-sidebar";

describe("studio sidebar interactions", () => {
  let container: HTMLDivElement;
  let root: Root;
  const mounted = vi.fn();
  const unmounted = vi.fn();
  function Child({ name }: { name: string }) {
    useEffect(() => { mounted(name); return () => { unmounted(name); }; }, [name]);
    return <input aria-label={name} defaultValue={name} />;
  }
  function Harness() {
    const [state, dispatch] = useReducer(reduceStudioSidebar, undefined, createStudioSidebarState);
    return <StudioSidebar state={state} onAction={dispatch} rooms={<Child name="rooms-child" />} assistants={<Child name="assistants-child" />} />;
  }
  beforeEach(async () => {
    vi.clearAllMocks();
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    await act(async () => root.render(<Harness />));
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });
  const panel = (name: string) => container.querySelector<HTMLElement>(`#studio-panel-${name}`)!;
  const button = (name: string) => container.querySelector<HTMLButtonElement>(`button[aria-label="${name}"]`)!;
  const separator = () => container.querySelector<HTMLElement>('[role="separator"]')!;
  const click = async (node: HTMLElement) => { await act(async () => node.click()); };
  const key = async (value: string) => { await act(async () => { separator().dispatchEvent(new KeyboardEvent("keydown", { key: value, bubbles: true })); }); };
  const width = () => separator().getAttribute("aria-valuenow");
  async function pointer(type: string, x: number, id = 1) {
    const event = new MouseEvent(type, { clientX: x, button: 0, bubbles: true });
    Object.defineProperty(event, "pointerId", { value: id });
    await act(async () => { separator().dispatchEvent(event); });
  }

  it("keeps both panel instances mounted through switches and folds", async () => {
    const rooms = panel("rooms").firstChild;
    const assistants = panel("assistants").firstChild;
    expect(mounted.mock.calls).toEqual([["rooms-child"], ["assistants-child"]]);
    await click(button("助手"));
    expect(panel("rooms").hidden).toBe(true);
    expect(panel("assistants").hidden).toBe(false);
    await click(button("助手"));
    expect(panel("assistants").hidden).toBe(true);
    await click(button("直播空间"));
    expect(panel("rooms").hidden).toBe(false);
    expect(panel("rooms").firstChild).toBe(rooms);
    expect(panel("assistants").firstChild).toBe(assistants);
    expect(mounted).toHaveBeenCalledTimes(2);
    expect(unmounted).not.toHaveBeenCalled();
  });

  it("supports bounded keyboard resize and restores activity focus on Escape", async () => {
    await click(button("助手"));
    await key("End");
    expect(width()).toBe("560");
    await key("ArrowRight");
    expect(width()).toBe("560");
    await key("Home");
    await key("ArrowLeft");
    expect(width()).toBe("320");
    await key("ArrowRight");
    expect(width()).toBe("344");
    await key("Enter");
    expect(width()).toBe("344");
    await key("Escape");
    expect(button("助手").getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(button("助手"));
    expect(separator().hidden).toBe(true);
    expect(separator().tabIndex).toBe(-1);
  });

  it("captures the resizing pointer, clamps width and ignores other or cancelled pointers", async () => {
    const capture = vi.fn();
    const release = vi.fn();
    separator().setPointerCapture = capture;
    separator().releasePointerCapture = release;
    await pointer("pointerdown", 384);
    expect(capture).toHaveBeenCalledWith(1);
    await pointer("pointermove", 500, 2);
    expect(width()).toBe("384");
    await pointer("pointermove", 500);
    expect(width()).toBe("500");
    await pointer("pointermove", 900);
    expect(width()).toBe("560");
    await pointer("pointerup", 900);
    expect(release).toHaveBeenCalledWith(1);
    await pointer("pointermove", 400);
    expect(width()).toBe("560");
    await pointer("pointerdown", 560);
    await pointer("pointercancel", 560);
    await pointer("pointermove", 360);
    expect(width()).toBe("560");
    await pointer("pointerdown", 560);
    await pointer("lostpointercapture", 560);
    await pointer("pointermove", 360);
    expect(width()).toBe("560");
  });
});
