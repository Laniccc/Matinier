// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HostActionPanel } from "./host-action-panel";
import type { HostActionDescriptor } from "@/types/host-actions";

const api = vi.hoisted(() => ({ createAssistantUIContext: vi.fn(), prepareHostAction: vi.fn(), confirmHostAction: vi.fn() }));
vi.mock("@/lib/api", () => api);

function descriptor(external = false): HostActionDescriptor {
  return { source_kind: "host_actions", plugin_id: "com.matinier.meeting-assistant", media_session_id: "m", plugin_version: "1.0.0",
    view_version: 2, authority_epoch: 1, analysis_epoch: 1, actions: [{ id: "action", action: external ? "meeting.execute" : "meeting.ask",
      label: external ? "执行到 Linear" : "询问", enabled: true, reason: null, fields: [], fixed_arguments: { message: "A question" } }] };
}
function preview(external = false) {
  return { preview_id: "preview", preview_hash: "hash", action: external ? "meeting.execute" : "meeting.ask", effect: external ? "external_write" : "local_write",
    confirmation_required: external, team_id: external ? "TEAM-1" : null, max_side_effects: external ? 1 : 0,
    candidates: [{ candidate_id: "candidate-1", content: { title: { value: "Release notes" } } }], expires_at: new Date(Date.now() + 60000).toISOString() };
}

describe("trusted Host action panel", () => {
  let container: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    api.createAssistantUIContext.mockResolvedValue({ ui_nonce: "memory-only" });
    api.prepareHostAction.mockResolvedValue(preview());
    api.confirmHostAction.mockResolvedValue({ status: "accepted", operation_id: "op-1" });
    container = document.createElement("div"); document.body.append(container); root = createRoot(container);
  });
  afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.unstubAllGlobals(); vi.useRealTimers(); });
  const render = async (value = descriptor()) => { await act(async () => root.render(<HostActionPanel descriptor={value} />)); };
  const click = async (text: string) => { const button = [...container.querySelectorAll("button")].find(item => item.textContent === text); expect(button).toBeDefined(); await act(async () => button!.click()); };

  it("does not prepare on mount and submits local actions without a second dialog", async () => {
    await render();
    expect(api.prepareHostAction).not.toHaveBeenCalled();
    await click("提交操作");
    expect(api.prepareHostAction).toHaveBeenCalledTimes(1);
    expect(api.confirmHostAction).toHaveBeenCalledWith("m", expect.objectContaining({ confirmed: true }), "memory-only");
    expect(container.textContent).toContain("已受理");
    expect(localStorage.length).toBe(0);
  });
  it("external writes show Host scope and require explicit confirmation", async () => {
    api.prepareHostAction.mockResolvedValue(preview(true));
    await render(descriptor(true));
    await click("提交操作");
    expect(api.confirmHostAction).not.toHaveBeenCalled();
    expect(container.textContent).toContain("TEAM-1");
    expect(container.textContent).toContain("Release notes");
    await click("确认执行");
    expect(api.confirmHostAction).toHaveBeenCalledTimes(1);
  });
  it("cancelling a preview never confirms authority", async () => {
    api.prepareHostAction.mockResolvedValue(preview(true));
    await render(descriptor(true)); await click("提交操作"); await click("取消");
    expect(api.confirmHostAction.mock.calls.every(call => call[1].confirmed === false)).toBe(true);
    expect(container.textContent).not.toContain("TEAM-1");
  });
  it("a late prepare after switching plugin/epoch cannot auto-confirm", async () => {
    let release!: (value: unknown) => void;
    api.prepareHostAction.mockImplementation(() => new Promise(resolve => { release = resolve; }));
    await render(); await click("提交操作");
    await render({ ...descriptor(), authority_epoch: 2 });
    await act(async () => release(preview()));
    expect(api.confirmHostAction).not.toHaveBeenCalled();
  });
  it("retries an uncertain confirmation with the same preview and request", async () => {
    api.confirmHostAction.mockRejectedValueOnce(new TypeError("network response lost"));
    await render(); await click("提交操作"); await click("重试原请求");
    expect(api.prepareHostAction).toHaveBeenCalledTimes(1);
    expect(api.confirmHostAction.mock.calls[0]).toEqual(api.confirmHostAction.mock.calls[1]);
  });
  it("retrying a failed prepare still requires external confirmation", async () => {
    api.prepareHostAction.mockRejectedValueOnce(new TypeError("lost")).mockResolvedValue(preview(true));
    await render(descriptor(true)); await click("提交操作"); await click("重试原请求");
    expect(api.confirmHostAction).not.toHaveBeenCalled();
    expect(api.prepareHostAction.mock.calls[0][1]).toEqual(api.prepareHostAction.mock.calls[1][1]);
    await click("确认执行");
    expect(api.confirmHostAction).toHaveBeenCalledTimes(1);
  });
  it("treats an invalid admission response as uncertain and retains the original preview", async () => {
    api.confirmHostAction.mockResolvedValueOnce({ status: "accepted" });
    await render(); await click("提交操作"); await click("重试原请求");
    expect(api.prepareHostAction).toHaveBeenCalledTimes(1);
    expect(api.confirmHostAction.mock.calls[0]).toEqual(api.confirmHostAction.mock.calls[1]);
  });
  it("rejects expired previews and clears temporary authority", async () => {
    api.prepareHostAction.mockResolvedValue({ ...preview(true), expires_at: new Date(Date.now() - 1).toISOString() });
    await render(descriptor(true)); await click("提交操作");
    expect(api.confirmHostAction).not.toHaveBeenCalled();
    expect(container.textContent).toContain("过期");
  });
  it("a plugin selection can prefill declared data but cannot submit or grant", async () => {
    const value = descriptor(); value.actions[0].fields = [{ name: "message", label: "问题", type: "textarea", required: true }];
    await act(async () => root.render(<HostActionPanel descriptor={value} selection={{ contextKey: "m:com.matinier.meeting-assistant:1.0.0:host_actions:1:1", actionId: "action",
      values: { message: "已输入的问题", intent_token: "untrusted", actor_id: "intruder" }, serial: "click-1" }} />));
    expect(container.querySelector("textarea")?.value).toBe("已输入的问题");
    expect(api.prepareHostAction).not.toHaveBeenCalled(); expect(api.confirmHostAction).not.toHaveBeenCalled();
    await click("提交操作");
    expect(api.prepareHostAction.mock.calls[0][1].arguments).toEqual({ message: "已输入的问题" });
  });
});
