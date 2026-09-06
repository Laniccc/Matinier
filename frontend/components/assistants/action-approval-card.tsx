"use client";

import type { AssistantExecutionStatus, PendingActionApproval } from "@/types/assistant";

export function ActionApprovalCard({
  approval,
  executionStatus = null,
  resolving = false,
  onApprove,
  onReject,
}: {
  approval: PendingActionApproval;
  executionStatus?: AssistantExecutionStatus | null;
  resolving?: boolean;
  onApprove: () => void;
  onReject: () => void;
}) {
  const pending = approval.status === "pending";
  const statusLabel = approvalStatusLabel(approval, executionStatus);
  const fields = [
    ["Title", approval.title],
    ["Team", approval.team],
    ["Owner", approval.owner],
    ["Due", approval.due],
  ].filter(([, value]) => Boolean(value));

  return (
    <section className="actionApprovalCard" aria-label="外部操作授权">
      <header>
        <span className="label">Agent 请求执行外部操作</span>
        <strong>{approval.action_label}</strong>
        <p>这是一次外部副作用，确认后才会创建受限 ActionGrant。</p>
      </header>
      {fields.length ? (
        <dl>
          {fields.map(([label, value]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      {approval.evidence_refs.length ? (
        <p>Evidence: {approval.evidence_refs.join(", ")}</p>
      ) : null}
      <p role="status">{statusLabel}</p>
      {pending ? (
        <div className="actionApprovalActions">
          <button type="button" disabled={resolving} onClick={onReject}>
            拒绝
          </button>
          <button type="button" className="primaryAction" disabled={resolving} onClick={onApprove}>
            允许
          </button>
        </div>
      ) : null}
    </section>
  );
}

function approvalStatusLabel(
  approval: PendingActionApproval,
  executionStatus: AssistantExecutionStatus | null,
): string {
  if (approval.status === "pending") {
    return resolvingLabel(executionStatus) ?? "等待你确认是否允许这次外部写入";
  }
  if (approval.status === "rejected") {
    return "Action cancelled / 用户已拒绝执行";
  }
  if (approval.status === "expired") {
    return "授权请求已过期，未执行外部写入";
  }
  if (executionStatus === "failed") {
    return "已授权，但执行失败";
  }
  if (executionStatus === "completed" || executionStatus === "partial") {
    return "已允许并完成外部操作";
  }
  if (executionStatus && ["queued", "planning", "executing", "observing", "waiting_external", "reconciling"].includes(executionStatus)) {
    return "已允许，正在执行";
  }
  return "已允许";
}

function resolvingLabel(executionStatus: AssistantExecutionStatus | null): string | null {
  return executionStatus === "cancelled" ? "执行已取消" : null;
}
