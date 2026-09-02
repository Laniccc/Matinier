import { describe, expect, it, vi } from "vitest";

import {
  FloatingCaptionWindowError,
  requestFloatingCaptionWindow,
} from "./floating-caption-window";

function hostWith(
  pictureInPicture?: {
    window: Window | null;
    requestWindow: (options?: DocumentPictureInPictureOptions) => Promise<Window>;
  },
): Window {
  return { documentPictureInPicture: pictureInPicture } as unknown as Window;
}

describe("requestFloatingCaptionWindow", () => {
  it("reports unsupported hosts", async () => {
    await expect(requestFloatingCaptionWindow(hostWith()))
      .rejects.toMatchObject({ code: "unsupported" });
  });

  it("focuses and reuses an existing open window", async () => {
    const focus = vi.fn();
    const existing = { closed: false, focus } as unknown as Window;
    const requestWindow = vi.fn();

    const result = await requestFloatingCaptionWindow(hostWith({
      window: existing,
      requestWindow,
    }));

    expect(result).toEqual({ window: existing, reused: true });
    expect(focus).toHaveBeenCalledOnce();
    expect(requestWindow).not.toHaveBeenCalled();
  });

  it("requests the approved initial size", async () => {
    const opened = { closed: false } as Window;
    const requestWindow = vi.fn().mockResolvedValue(opened);

    const result = await requestFloatingCaptionWindow(hostWith({
      window: null,
      requestWindow,
    }));

    expect(requestWindow).toHaveBeenCalledWith({ width: 720, height: 180 });
    expect(result).toEqual({ window: opened, reused: false });
  });

  it.each([
    ["NotAllowedError", "not-allowed"],
    ["NotSupportedError", "not-supported"],
  ] as const)("maps %s to %s", async (name, code) => {
    const error = new Error(name);
    error.name = name;
    const host = hostWith({
      window: null,
      requestWindow: vi.fn().mockRejectedValue(error),
    });

    await expect(requestFloatingCaptionWindow(host))
      .rejects.toMatchObject({ code });
  });

  it("maps arbitrary failures without exposing the raw error", async () => {
    const raw = { secret: "must not leak" };
    const host = hostWith({
      window: null,
      requestWindow: vi.fn().mockRejectedValue(raw),
    });

    const caught = await requestFloatingCaptionWindow(host).catch(
      (error: unknown) => error,
    );

    expect(caught).toBeInstanceOf(FloatingCaptionWindowError);
    expect(caught).toMatchObject({ code: "open-failed" });
    expect(caught).not.toHaveProperty("cause");
  });
});
