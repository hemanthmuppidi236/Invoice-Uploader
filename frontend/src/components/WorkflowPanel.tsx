"use client";

/**
 * The invoice workflow action panel (prompt §7.3–7.5, §10).
 *
 * §10: "The action panel renders nothing when the role/status combo has no
 * permission." That is why this returns null rather than rendering disabled
 * buttons — a greyed-out Approve button on somebody else's invoice is an
 * invitation to ask why it does not work.
 *
 * The exception is a blocker the person can actually clear: an unbalanced
 * cost split, or a missing approver. Those render as an explanation, because
 * hiding the button there just makes the screen look broken.
 */

import { useState } from "react";
import {
  userLabel,
  workflowActions,
  type AppUser,
  type CurrentUser,
  type InvoiceDetail,
} from "@/lib/types";

export function WorkflowPanel({
  invoice,
  user,
  users,
  busy,
  onAssign,
  onReassign,
  onReview,
  onApprove,
  onReject,
}: {
  invoice: InvoiceDetail;
  user: CurrentUser | null;
  users: AppUser[];
  busy: boolean;
  onAssign: (reviewerId: string, approverId: string | null) => void;
  onReassign: (reviewerId: string | null, approverId: string | null) => void;
  onReview: (approverId: string | null) => void;
  onApprove: () => void;
  onReject: (reason: string) => void;
}) {
  const actions = workflowActions(user, invoice);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [reviewerDraft, setReviewerDraft] = useState(invoice.reviewer_id ?? "");
  const [approverDraft, setApproverDraft] = useState(invoice.approver_id ?? "");

  const reviewers = users.filter((u) =>
    u.roles.some((r) => r === "pe" || r === "accountant")
  );
  const approvers = users.filter((u) => u.roles.includes("approver"));

  const nothingToShow =
    !actions.canAssign &&
    !actions.canReassign &&
    !actions.canReview &&
    !actions.canApprove &&
    !actions.reviewBlockedBecause &&
    !actions.approveBlockedBecause;

  if (nothingToShow) return null;

  return (
    <div className="glass section-card" style={{ marginBottom: 18 }}>
      <div className="section-header">
        <div className="section-title">
          {invoice.status === "pending_approval"
            ? "Approval"
            : invoice.status === "assigned"
              ? "Review"
              : "Assignment"}
        </div>
      </div>

      {/* ─── Assign (§7.3) ──────────────────────────────────────── */}
      {actions.canAssign && (
        <>
          <div className="callout callout-info">
            <div className="callout-title">Route this invoice</div>
            The reviewer defaults to the project&rsquo;s engineer and the
            approver to the project&rsquo;s default, both filled in when the AI
            read the invoice. Change either here.
          </div>

          <div className="form-grid-2">
            <div className="form-row">
              <label className="form-label" htmlFor="assign-reviewer">
                Reviewer
              </label>
              <select
                id="assign-reviewer"
                className="input"
                value={reviewerDraft}
                disabled={busy}
                onChange={(e) => setReviewerDraft(e.target.value)}
              >
                <option value="">Pick a reviewer</option>
                {reviewers.map((u) => (
                  <option key={u.id} value={u.id}>
                    {userLabel(u)}
                  </option>
                ))}
              </select>
              {!invoice.project_id && (
                <div className="form-help">
                  No project is matched, so there is no project engineer to
                  default to.
                </div>
              )}
            </div>

            <div className="form-row">
              <label className="form-label" htmlFor="assign-approver">
                Approver
              </label>
              <select
                id="assign-approver"
                className="input"
                value={approverDraft}
                disabled={busy}
                onChange={(e) => setApproverDraft(e.target.value)}
              >
                <option value="">Decide at review time</option>
                {approvers.map((u) => (
                  <option key={u.id} value={u.id}>
                    {userLabel(u)}
                  </option>
                ))}
              </select>
              <div className="form-help">
                Optional now — the reviewer can set it when marking reviewed.
              </div>
            </div>
          </div>

          <div className="form-actions">
            <button
              className="btn btn-accent"
              disabled={busy || !reviewerDraft}
              onClick={() => onAssign(reviewerDraft, approverDraft || null)}
            >
              {busy ? "Assigning…" : "Assign for review"}
            </button>
          </div>
        </>
      )}

      {/* ─── Reassign ───────────────────────────────────────────── */}
      {actions.canReassign && (
        <>
          <div className="callout callout-info">
            <div className="callout-title">Reassign</div>
            The invoice stays where it is; only the person changes. Use this
            when the assigned approver is unavailable — it is recorded, which
            is why approving itself insists on the person who was asked.
          </div>

          <div className="form-grid-2">
            <div className="form-row">
              <label className="form-label" htmlFor="re-reviewer">
                Reviewer
              </label>
              <select
                id="re-reviewer"
                className="input"
                value={reviewerDraft}
                disabled={busy}
                onChange={(e) => setReviewerDraft(e.target.value)}
              >
                {reviewers.map((u) => (
                  <option key={u.id} value={u.id}>
                    {userLabel(u)}
                  </option>
                ))}
              </select>
            </div>
            <div className="form-row">
              <label className="form-label" htmlFor="re-approver">
                Approver
              </label>
              <select
                id="re-approver"
                className="input"
                value={approverDraft}
                disabled={busy}
                onChange={(e) => setApproverDraft(e.target.value)}
              >
                <option value="">Not set</option>
                {approvers.map((u) => (
                  <option key={u.id} value={u.id}>
                    {userLabel(u)}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="form-actions">
            <button
              className="btn"
              disabled={
                busy ||
                (reviewerDraft === (invoice.reviewer_id ?? "") &&
                  approverDraft === (invoice.approver_id ?? ""))
              }
              onClick={() =>
                onReassign(reviewerDraft || null, approverDraft || null)
              }
            >
              Save the reassignment
            </button>
          </div>
        </>
      )}

      {/* ─── Review (§7.4) ──────────────────────────────────────── */}
      {invoice.status === "assigned" && (
        <>
          {actions.canReview && (
            <>
              <div className="callout callout-ok">
                <div className="callout-title">Your turn</div>
                Check the values against the PDF, correct anything wrong, and
                confirm the cost code. Marking reviewed sends it to{" "}
                {invoice.approver_name || "the approver"} — nothing reaches
                BuilderTrend until they approve it.
              </div>

              <div className="form-row">
                <label className="form-label" htmlFor="review-approver">
                  Who should approve this?
                </label>
                <select
                  id="review-approver"
                  className="input"
                  value={approverDraft}
                  disabled={busy}
                  onChange={(e) => setApproverDraft(e.target.value)}
                >
                  <option value="">Pick an approver</option>
                  {approvers.map((u) => (
                    <option key={u.id} value={u.id}>
                      {userLabel(u)}
                    </option>
                  ))}
                </select>
              </div>

              <div className="form-actions">
                <button
                  className="btn btn-accent"
                  disabled={busy || !approverDraft}
                  onClick={() => onReview(approverDraft || null)}
                >
                  {busy ? "Saving…" : "Mark reviewed"}
                </button>
              </div>
            </>
          )}

          {!actions.canReview && actions.reviewBlockedBecause && (
            <div className="callout callout-warn">
              <div className="callout-title">Not ready to mark reviewed</div>
              {actions.reviewBlockedBecause}
            </div>
          )}
        </>
      )}

      {/* ─── Approve or reject (§7.5) ───────────────────────────── */}
      {invoice.status === "pending_approval" && (
        <>
          {actions.canApprove && (
            <>
              <div className="callout callout-ok">
                <div className="callout-title">Awaiting your approval</div>
                {invoice.reviewer_name || "The reviewer"} has checked these
                values. Approving releases the invoice to the next BuilderTrend
                upload session.
              </div>

              <div
                style={{ display: "flex", gap: 8, flexWrap: "wrap" }}
              >
                <button
                  className="btn btn-accent"
                  disabled={busy}
                  onClick={onApprove}
                >
                  {busy ? "Approving…" : "Approve"}
                </button>
                <button
                  className="btn dash-btn-red"
                  disabled={busy}
                  onClick={() => setRejecting((v) => !v)}
                >
                  {rejecting ? "Cancel" : "Reject"}
                </button>
              </div>
            </>
          )}

          {!actions.canApprove && actions.approveBlockedBecause && (
            <div className="callout callout-warn">
              <div className="callout-title">Cannot approve</div>
              {actions.approveBlockedBecause}
            </div>
          )}

          {rejecting && (
            <div style={{ marginTop: 16 }}>
              <div className="form-row">
                <label className="form-label" htmlFor="reject-reason">
                  What needs to change?
                </label>
                <textarea
                  id="reject-reason"
                  className="input"
                  rows={3}
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  placeholder="This was a deck pour, not columns — mix 6011000 is the shear wall mix."
                />
                <div className="form-help">
                  Required. It goes back to{" "}
                  {invoice.reviewer_name || "the same reviewer"} with this
                  reason in their banner.
                </div>
              </div>
              <button
                className="btn dash-btn-red"
                disabled={busy || !reason.trim()}
                onClick={() => {
                  onReject(reason.trim());
                  setRejecting(false);
                  setReason("");
                }}
              >
                Send it back
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
