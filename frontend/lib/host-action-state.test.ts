import { describe, expect, it } from "vitest";
import { createHostActionState, reduceHostAction, hostContextKey, buildHostArguments } from "./host-action-state";

describe("Host action reducer", () => {
  it("ignores late responses from a previous session or operation", () => {
    const idle = createHostActionState("session-A");
    const preparing = reduceHostAction(idle, { type: "start", requestId: "request-A" });
    const switched = reduceHostAction(preparing, { type: "reset", contextKey: "session-B" });
    expect(reduceHostAction(switched, { type: "accepted", requestId: "request-A", operationId: "old" })).toEqual(switched);
  });
  it("keeps one request identity through uncertain submission and retry", () => {
    let state = reduceHostAction(createHostActionState("a"), { type: "start", requestId: "stable" });
    state = reduceHostAction(state, { type: "phase", requestId: "stable", phase: "submitting" });
    state = reduceHostAction(state, { type: "error", requestId: "stable", message: "unknown", retryable: true });
    expect(state.requestId).toBe("stable");
    state = reduceHostAction(state, { type: "phase", requestId: "stable", phase: "submitting" });
    expect(state.phase).toBe("submitting");
  });
  it("clears previews on cancel, expiry, and scope reset", () => {
    const state = reduceHostAction(createHostActionState("a"), { type: "start", requestId: "r" });
    expect(reduceHostAction(state, { type: "reset", contextKey: "a" }).requestId).toBeNull();
    expect(hostContextKey({ media_session_id: "a", plugin_id: "p", plugin_version: "1", authority_epoch: 1, analysis_epoch: 2, source_kind: "host_actions" })).not.toBe(
      hostContextKey({ media_session_id: "a", plugin_id: "p", plugin_version: "1", authority_epoch: 2, analysis_epoch: 2, source_kind: "host_actions" }));
  });
  it("uses only fields and option bindings supplied by the Host", () => {
    const action = { id: "mark", action: "meeting.mark.accept", label: "Accept", enabled: true, reason: null,
      fixed_arguments: {}, fields: [{ name: "mark_id", label: "Mark", type: "select" as const, required: true,
        options: [{ value: "m", label: "M", arguments: { expected_state_version: 2 } }] }] };
    expect(buildHostArguments(action, { mark_id: "m", actor_id: "admin", intent_token: "forged" })).toEqual({ mark_id: "m", expected_state_version: 2 });
    expect(() => buildHostArguments(action, { mark_id: "foreign" })).toThrow();
  });
});
