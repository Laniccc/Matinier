// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ActionApprovalCard } from "./action-approval-card";
import type { PendingActionApproval } from "@/types/assistant";

function approval(status: PendingActionApproval["status"] = "pending"): PendingActionApproval {
  return {
    approval_id: "approval-1",
    execution_id: "exec-1",
    capability: "task.create",
    action_label: "创建 Linear 任务",
    title: "数据库迁移",
    team: "Backend",
    owner: "张三",
    due: "Sunday",
    candidate_id: "candidate-1",
    evidence_refs: ["F201", "F202"],
    status,
    expires_at: "2030-01-01T00:00:00Z",
    created_at: "2026-09-06T00:00:00Z",
    resolved_at: status === "pending" ? null : "2026-09-06T00:01:00Z",
    grant_id: status === "approved" ? "grant-1" : null,
  };
}

describe("ActionApprovalCard", () => {
  let container: HTMLDivElement;
  let root: Root;
  const onApprove = vi.fn();
  const onReject = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  const render = async (
    value = approval(),
    extras: { resolving?: boolean; executionStatus?: "planning" | "cancelled" | "completed" } = {},
  ) => {
    await act(async () => root.render(
      <ActionApprovalCard
        approval={value}
        resolving={extras.resolving}
        executionStatus={extras.executionStatus ?? null}
        onApprove={onApprove}
        onReject={onReject}
      />,
    ));
  };

  const click = async (text: string) => {
    const button = [...container.querySelectorAll("button")].find((item) => item.textContent === text);
    expect(button).toBeDefined();
    await act(async () => button!.click());
  };

  it("shows the pending external action details", async () => {
    await render();
    expect(container.textContent).toContain("Agent 请求执行外部操作");
    expect(container.textContent).toContain("创建 Linear 任务");
    expect(container.textContent).toContain("数据库迁移");
    expect(container.textContent).toContain("Backend");
    expect(container.textContent).toContain("张三");
    expect(container.textContent).toContain("Sunday");
    expect(container.textContent).toContain("F201, F202");
    expect(container.textContent).toContain("外部副作用");
  });

  it("invokes Approve and Reject callbacks", async () => {
    await render();
    await click("允许");
    await click("拒绝");
    expect(onApprove).toHaveBeenCalledTimes(1);
    expect(onReject).toHaveBeenCalledTimes(1);
  });

  it("disables buttons while resolving", async () => {
    await render(approval(), { resolving: true });
    const buttons = [...container.querySelectorAll("button")];
    expect(buttons).toHaveLength(2);
    expect(buttons.every((button) => button.disabled)).toBe(true);
    await click("允许");
    expect(onApprove).not.toHaveBeenCalled();
  });

  it("shows approved and rejected results without repeating the action buttons", async () => {
    await render(approval("approved"), { executionStatus: "completed" });
    expect(container.textContent).toContain("已允许并完成外部操作");
    expect(container.querySelectorAll("button")).toHaveLength(0);

    await render(approval("rejected"));
    expect(container.textContent).toContain("用户已拒绝执行");
    expect(container.querySelectorAll("button")).toHaveLength(0);
  });
});
