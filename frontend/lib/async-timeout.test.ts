import { afterEach, describe, expect, it, vi } from "vitest";

import { withTimeout } from "./async-timeout";

describe("withTimeout", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("returns an operation result and clears the timeout", async () => {
    vi.useFakeTimers();

    await expect(
      withTimeout(Promise.resolve("published"), 15_000, "timed out"),
    ).resolves.toBe("published");
    expect(vi.getTimerCount()).toBe(0);
  });

  it("rejects when an operation does not settle before the deadline", async () => {
    vi.useFakeTimers();
    const pending = new Promise<never>(() => undefined);
    const expectation = expect(
      withTimeout(pending, 15_000, "音轨发布超时"),
    ).rejects.toThrow("音轨发布超时");

    await vi.advanceTimersByTimeAsync(15_000);

    await expectation;
    expect(vi.getTimerCount()).toBe(0);
  });

  it("rejects invalid timeout values immediately", async () => {
    await expect(
      withTimeout(Promise.resolve("unused"), 0, "timed out"),
    ).rejects.toThrow(RangeError);
  });
});
