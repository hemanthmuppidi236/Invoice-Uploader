"use client";

/**
 * Invoice detail — `/invoices/[id]` (prompt §10)
 *
 * Two columns. The PDF sticks on the left while the form scrolls on the
 * right, because the reviewer's actual task is reading a value off the
 * invoice and checking it against the extracted field beside it.
 *
 * The cost split auto-saves 900 ms after typing while the invoice is the
 * reviewer's or Linda's turn (§10), matching the pay app's billing cells.
 * Everything else saves on blur: a stray keystroke in a reference field
 * should be recoverable, and an amount is not.
 *
 * The workflow panel renders nothing when the role and status combination has
 * no permission, same as the pay app.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { api, formatApiError } from "@/lib/api";
import { ErrorBanner } from "@/components/ErrorBanner";
import { WorkflowPanel } from "@/components/WorkflowPanel";
import { useCurrentUser } from "@/lib/useCurrentUser";
import {
  FLAG_LABELS,
  INVOICE_STATUS_LABELS,
  actorLabel,
  type AppUser,
  confidenceLabel,
  confidencePillClass,
  fmtDate,
  fmtDateTime,
  fmtMoney,
  hasRole,
  statusPillClass,
  type CostCode,
  type FlagCode,
  type InvoiceDetail,
  type Project,
  type SignedUrl,
  type Vendor,
} from "@/lib/types";

/** §10: the detail form is editable only while the record is somebody's turn. */
const EDITABLE_STATUSES = ["ingested", "suggested", "assigned", "flagged"];

interface CostDraft {
  cost_code_id: string;
  amount: string;
}

export default function InvoiceDetailPage() {
  const params = useParams<{ id: string }>();
  const invoiceId = params?.id ?? "";
  const { user } = useCurrentUser();

  const [invoice, setInvoice] = useState<InvoiceDetail | null>(null);
  const [pdfUrl, setPdfUrl] = useState<string | null>(null);
  const [codes, setCodes] = useState<CostCode[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [vendors, setVendors] = useState<Vendor[]>([]);
  const [users, setUsers] = useState<AppUser[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [showRationale, setShowRationale] = useState(true);
  const [costDraft, setCostDraft] = useState<CostDraft[] | null>(null);

  const canEdit =
    hasRole(user, "admin", "accountant", "pe") &&
    !!invoice &&
    EDITABLE_STATUSES.includes(invoice.status);
  const canTriage = hasRole(user, "admin", "accountant");

  const reload = useCallback(async () => {
    try {
      const fresh = await api.get<InvoiceDetail>(`/invoices/${invoiceId}`);
      setInvoice(fresh);
      setCostDraft(
        fresh.costs.map((c) => ({
          cost_code_id: c.cost_code_id,
          amount: String(c.amount),
        }))
      );
    } catch (e) {
      setError(formatApiError(e));
    }
  }, [invoiceId]);

  useEffect(() => {
    if (!invoiceId) return;
    reload();
    Promise.all([
      api.get<CostCode[]>("/admin/cost-codes?active_only=true"),
      api.get<Project[]>("/projects"),
      api.get<Vendor[]>("/admin/vendors?active_only=true"),
      api.get<AppUser[]>("/admin/users"),
    ])
      .then(([c, p, v, u]) => {
        setCodes(c);
        setProjects(p);
        setVendors(v);
        setUsers(u);
      })
      .catch((e) => setError(formatApiError(e)));
  }, [invoiceId, reload]);

  // The signed URL expires in an hour, so it is fetched per visit rather
  // than stored alongside the invoice.
  useEffect(() => {
    if (!invoiceId || !invoice?.pdf_storage_path) return;
    api
      .get<SignedUrl>(`/invoices/${invoiceId}/pdf-url`)
      .then(({ url }) => setPdfUrl(url))
      .catch(() => setPdfUrl(null));
  }, [invoiceId, invoice?.pdf_storage_path]);

  const draftTotal = useMemo(() => {
    if (!costDraft) return 0;
    return costDraft.reduce((sum, r) => sum + (parseFloat(r.amount) || 0), 0);
  }, [costDraft]);

  const draftBalanced = useMemo(() => {
    if (!invoice || invoice.amount === null || !costDraft) return false;
    return Math.abs(draftTotal - invoice.amount) < 0.005;
  }, [invoice, costDraft, draftTotal]);

  /**
   * §10: auto-save 900 ms after typing, only while the record is somebody's
   * turn. Compared against the server's rows rather than a dirty flag —
   * patch() replaces the draft from the response, and a flag would re-fire
   * the effect on its own write and loop.
   */
  const serverCostSignature = useMemo(
    () =>
      (invoice?.costs ?? [])
        .map((c) => `${c.cost_code_id}:${Number(c.amount).toFixed(2)}`)
        .join("|"),
    [invoice]
  );
  const draftCostSignature = useMemo(
    () =>
      (costDraft ?? [])
        .filter((r) => r.cost_code_id)
        .map((r) => `${r.cost_code_id}:${(parseFloat(r.amount) || 0).toFixed(2)}`)
        .join("|"),
    [costDraft]
  );
  const autoSaveOn =
    canEdit && !!invoice && ["assigned", "flagged"].includes(invoice.status);
  const [autoSaveState, setAutoSaveState] = useState<
    "idle" | "pending" | "saving" | "saved"
  >("idle");

  useEffect(() => {
    if (!autoSaveOn || costDraft === null) return;
    if (draftCostSignature === serverCostSignature) return;
    // A half-typed row would save a cost code with no amount, so wait until
    // every row the reviewer has started has both halves.
    if (costDraft.some((r) => !r.cost_code_id)) return;

    setAutoSaveState("pending");
    const timer = setTimeout(async () => {
      setAutoSaveState("saving");
      try {
        const fresh = await api.patch<InvoiceDetail>(`/invoices/${invoiceId}`, {
          costs: costDraft.map((r) => ({
            cost_code_id: r.cost_code_id,
            amount: parseFloat(r.amount) || 0,
          })),
        });
        setInvoice(fresh);
        setAutoSaveState("saved");
      } catch (e) {
        setError(formatApiError(e));
        setAutoSaveState("idle");
      }
    }, 900);
    return () => clearTimeout(timer);
  }, [
    autoSaveOn,
    costDraft,
    draftCostSignature,
    serverCostSignature,
    invoiceId,
  ]);

  async function patch(changes: Record<string, unknown>) {
    setBusy(true);
    setError(null);
    try {
      const fresh = await api.patch<InvoiceDetail>(
        `/invoices/${invoiceId}`,
        changes
      );
      setInvoice(fresh);
      setCostDraft(
        fresh.costs.map((c) => ({
          cost_code_id: c.cost_code_id,
          amount: String(c.amount),
        }))
      );
      setNotice("Saved.");
    } catch (e) {
      setError(formatApiError(e));
    } finally {
      setBusy(false);
    }
  }

  async function act(path: string, body?: unknown, message?: string) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const fresh = await api.post<InvoiceDetail>(
        `/invoices/${invoiceId}/${path}`,
        body
      );
      setInvoice(fresh);
      setCostDraft(
        fresh.costs.map((c) => ({
          cost_code_id: c.cost_code_id,
          amount: String(c.amount),
        }))
      );
      if (message) setNotice(message);
    } catch (e) {
      setError(formatApiError(e));
    } finally {
      setBusy(false);
    }
  }

  function saveCosts() {
    if (!costDraft) return;
    patch({
      costs: costDraft
        .filter((r) => r.cost_code_id)
        .map((r) => ({
          cost_code_id: r.cost_code_id,
          amount: parseFloat(r.amount) || 0,
        })),
    });
  }

  /** §10: alternatives render as one-click chips. */
  function applyAlternative(costCodeId: string) {
    if (!invoice || invoice.amount === null) return;
    patch({
      costs: [{ cost_code_id: costCodeId, amount: invoice.amount }],
    });
  }

  if (!invoice) {
    return (
      <div className="page-content" style={{ paddingTop: 36 }}>
        {error ? (
          <ErrorBanner message={error} onDismiss={() => setError(null)} />
        ) : (
          <div style={{ color: "var(--text-muted)" }}>Loading…</div>
        )}
      </div>
    );
  }

  return (
    <>
      <div className="page-header">
        <div className="page-title-block">
          <div className="page-eyebrow">
            {invoice.vendor_name || "Unknown vendor"}
            {invoice.vendor_bt_name &&
              invoice.vendor_bt_name !== invoice.vendor_name &&
              ` → ${invoice.vendor_bt_name} in BuilderTrend`}
          </div>
          <h1 className="page-title">
            {invoice.invoice_no || invoice.source_filename || "Invoice"}
          </h1>
          <div className="page-meta">
            {invoice.project_name || "No project matched"}
            {invoice.project_no ? ` · ${invoice.project_no}` : ""}
            {" · "}
            <strong>{fmtMoney(invoice.amount)}</strong>
            {invoice.is_credit && " credit memo"}
          </div>
        </div>
        <div className="page-actions">
          <Link href="/invoices" className="btn btn-ghost">
            All invoices
          </Link>
        </div>
      </div>

      <div className="page-content">
        {error && <ErrorBanner message={error} onDismiss={() => setError(null)} />}
        {notice && (
          <div className="callout callout-ok">
            <div className="callout-title">Done</div>
            {notice}
          </div>
        )}

        <div className="inv-grid">
          {/* ─── Left: the PDF ──────────────────────────────────── */}
          <div className="glass pdf-pane">
            <div className="pdf-bar">
              <span className="mono">
                {invoice.source_filename || "no filename"}
              </span>
              {invoice.source_path && (
                <span className="mono" style={{ color: "var(--text-faint)" }}>
                  {invoice.source_path}
                </span>
              )}
              {pdfUrl && (
                <a
                  href={pdfUrl}
                  target="_blank"
                  rel="noopener"
                  className="dash-open-link"
                  style={{ marginLeft: "auto" }}
                >
                  Open full size
                </a>
              )}
            </div>
            {pdfUrl ? (
              <iframe
                className="pdf-frame"
                src={pdfUrl}
                title="Invoice PDF"
              />
            ) : (
              <div className="pdf-missing">
                {invoice.pdf_storage_path
                  ? "Fetching the PDF…"
                  : "No PDF is stored for this invoice — intake did not finish downloading it from Drive."}
              </div>
            )}
          </div>

          {/* ─── Right: status, fields, coding ──────────────────── */}
          <div>
            <StatusBanner invoice={invoice} />

            {/* Fields */}
            <div className="glass section-card" style={{ marginBottom: 18 }}>
              <div className="section-header">
                <div className="section-title">Invoice</div>
                {!canEdit && <span className="pill pill-muted">Read only</span>}
              </div>

              <div className="form-grid-2">
                <Field
                  label="Invoice number"
                  value={invoice.invoice_no ?? ""}
                  canEdit={canEdit}
                  busy={busy}
                  onSave={(v) => patch({ invoice_no: v || null })}
                  help={
                    invoice.bill_no
                      ? `BuilderTrend Bill # will be ${invoice.bill_no} — the last four digits.`
                      : "No digits found, so there is no Bill # to enter."
                  }
                />
                <Field
                  label="Amount"
                  value={invoice.amount === null ? "" : String(invoice.amount)}
                  canEdit={canEdit}
                  busy={busy}
                  numeric
                  onSave={(v) => patch({ amount: v === "" ? null : parseFloat(v) })}
                  help="Goes in the Costs row as Unit cost, with Qty 1."
                />
                <Field
                  label="Invoice date"
                  value={invoice.invoice_date ?? ""}
                  canEdit={canEdit}
                  busy={busy}
                  type="date"
                  onSave={(v) => patch({ invoice_date: v || null })}
                  help={
                    invoice.due_date
                      ? `Due ${fmtDate(invoice.due_date)} — end of the month after, per the SOP.`
                      : "The due date is derived from this."
                  }
                />
                <div className="form-row">
                  <label className="form-label">Due date</label>
                  <div style={{ fontSize: 14 }}>{fmtDate(invoice.due_date)}</div>
                  <div className="form-help">
                    Computed from the invoice date. Vendor terms are ignored.
                  </div>
                </div>

                <div className="form-row">
                  <label className="form-label">Project</label>
                  {canEdit ? (
                    <select
                      className="input"
                      value={invoice.project_id ?? ""}
                      disabled={busy}
                      onChange={(e) =>
                        patch({ project_id: e.target.value || null })
                      }
                    >
                      <option value="">Not matched</option>
                      {projects.map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.project_no} — {p.name}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <div style={{ fontSize: 14 }}>
                      {invoice.project_name || "—"}
                    </div>
                  )}
                </div>

                <div className="form-row">
                  <label className="form-label">Vendor</label>
                  {canEdit ? (
                    <select
                      className="input"
                      value={invoice.vendor_id ?? ""}
                      disabled={busy}
                      onChange={(e) =>
                        patch({ vendor_id: e.target.value || null })
                      }
                    >
                      <option value="">Not matched</option>
                      {vendors.map((v) => (
                        <option key={v.id} value={v.id}>
                          {v.invoice_name} → {v.bt_name}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <div style={{ fontSize: 14 }}>
                      {invoice.vendor_name || "—"}
                    </div>
                  )}
                </div>
              </div>

              {canEdit && (
                <div className="form-checkbox" style={{ marginTop: 6 }}>
                  <input
                    id="is-credit"
                    type="checkbox"
                    checked={invoice.is_credit}
                    disabled={busy}
                    onChange={(e) => patch({ is_credit: e.target.checked })}
                  />
                  <label htmlFor="is-credit" style={{ fontSize: 13.5 }}>
                    Credit memo
                    <span
                      style={{
                        display: "block",
                        fontSize: 12.5,
                        color: "var(--text-faint)",
                        marginTop: 3,
                      }}
                    >
                      Entered as a normal bill with a negative amount, on the
                      same job.
                    </span>
                  </label>
                </div>
              )}
            </div>

            {/* AI rationale */}
            {invoice.suggestion && (
              <div className="rationale">
                <button
                  className="rationale-head"
                  onClick={() => setShowRationale((v) => !v)}
                >
                  <span className="rationale-label">Why this code</span>
                  <span
                    className={`pill ${confidencePillClass(
                      invoice.suggestion.confidence
                    )}`}
                  >
                    {confidenceLabel(invoice.suggestion.confidence)}
                  </span>
                  <span className="rationale-caret">
                    {showRationale ? "Hide" : "Show"}
                  </span>
                </button>

                {showRationale && (
                  <div className="rationale-body">
                    <div style={{ marginBottom: 10 }}>
                      <span className="mono">
                        {invoice.suggestion.code || "no code suggested"}
                      </span>
                    </div>
                    {invoice.suggestion.rationale || "No rationale given."}

                    {invoice.suggestion.alternatives.length > 0 && (
                      <div style={{ marginTop: 14 }}>
                        <div className="callout-title">Alternatives</div>
                        <div
                          style={{
                            display: "flex",
                            flexDirection: "column",
                            gap: 8,
                          }}
                        >
                          {invoice.suggestion.alternatives.map((alt) => (
                            <div key={alt.cost_code_id}>
                              <button
                                className="alt-chip"
                                disabled={!canEdit || busy || invoice.amount === null}
                                onClick={() => applyAlternative(alt.cost_code_id)}
                                title={
                                  invoice.amount === null
                                    ? "The amount has to be known before a code can be applied."
                                    : "Use this code for the whole invoice"
                                }
                              >
                                {alt.code}
                              </button>
                              <span className="cell-sub">{alt.why}</span>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}

                    <div
                      style={{
                        marginTop: 12,
                        fontSize: 11.5,
                        color: "var(--text-faint)",
                      }}
                    >
                      Read by {invoice.suggestion.model || "the model"}
                      {invoice.suggestion.created_at &&
                        ` · ${fmtDateTime(invoice.suggestion.created_at)}`}
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* Cost split */}
            <div className="glass section-card" style={{ marginBottom: 18 }}>
              <div className="section-header">
                <div className="section-title">Cost coding</div>
                {invoice.costs.length > 1 && (
                  <span className="pill pill-blue">
                    Split across {invoice.costs.length}
                  </span>
                )}
              </div>

              {invoice.costs[0]?.base_code && (
                <div className="callout callout-info">
                  <div className="callout-title">In BuilderTrend</div>
                  The Bill&rsquo;s Title field takes{" "}
                  <span className="mono">{invoice.costs[0].base_code}</span>.
                  Each row below becomes one Costs row, with the amount as Unit
                  cost and Qty 1. Leave the Costs row Title blank.
                </div>
              )}

              {costDraft?.map((row, i) => (
                <div key={i} className="cost-row">
                  <select
                    className="cell-select"
                    value={row.cost_code_id}
                    disabled={!canEdit || busy}
                    onChange={(e) =>
                      setCostDraft(
                        costDraft.map((r, j) =>
                          j === i ? { ...r, cost_code_id: e.target.value } : r
                        )
                      )
                    }
                  >
                    <option value="">Pick a cost code</option>
                    {codes.map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.code}
                      </option>
                    ))}
                  </select>
                  <input
                    className="cell-select"
                    style={{ textAlign: "right", minWidth: 0 }}
                    value={row.amount}
                    disabled={!canEdit || busy}
                    inputMode="decimal"
                    onChange={(e) =>
                      setCostDraft(
                        costDraft.map((r, j) =>
                          j === i
                            ? {
                                ...r,
                                // Digits, one decimal point, optional leading
                                // minus for a credit memo.
                                amount: e.target.value.replace(
                                  /[^0-9.-]/g,
                                  ""
                                ),
                              }
                            : r
                        )
                      )
                    }
                  />
                  {canEdit && (
                    <button
                      className="btn btn-ghost"
                      style={{ padding: "4px 8px", fontSize: 12 }}
                      disabled={busy}
                      onClick={() =>
                        setCostDraft(costDraft.filter((_, j) => j !== i))
                      }
                      aria-label="Remove this cost row"
                    >
                      ✕
                    </button>
                  )}
                </div>
              ))}

              <div
                className={`cost-total ${
                  draftBalanced ? "cost-total-ok" : "cost-total-off"
                }`}
              >
                <span>
                  {draftBalanced
                    ? "Balances against the invoice total"
                    : `Off by ${fmtMoney(draftTotal - (invoice.amount ?? 0))}`}
                </span>
                <span>
                  {fmtMoney(draftTotal)} of {fmtMoney(invoice.amount)}
                </span>
              </div>

              {!draftBalanced && (
                <div className="callout callout-warn" style={{ marginTop: 12 }}>
                  <div className="callout-title">Blocks approval</div>
                  The cost rows must sum to the invoice total before this can
                  be approved.
                </div>
              )}

              {canEdit && (
                <div className="form-actions">
                  <button
                    className="btn"
                    disabled={busy}
                    onClick={() =>
                      setCostDraft([
                        ...(costDraft ?? []),
                        { cost_code_id: "", amount: "0" },
                      ])
                    }
                  >
                    Add a cost row
                  </button>
                  {autoSaveOn ? (
                    <span
                      style={{
                        fontSize: 12.5,
                        color:
                          autoSaveState === "saved"
                            ? "var(--status-green)"
                            : "var(--text-faint)",
                      }}
                    >
                      {autoSaveState === "pending" && "Saving shortly…"}
                      {autoSaveState === "saving" && "Saving…"}
                      {autoSaveState === "saved" && "Saved"}
                      {autoSaveState === "idle" && "Changes save automatically"}
                    </span>
                  ) : (
                    <button
                      className="btn btn-accent"
                      disabled={busy}
                      onClick={saveCosts}
                    >
                      {busy ? "Saving…" : "Save coding"}
                    </button>
                  )}
                </div>
              )}
            </div>

            {/* Workflow: assign, review, approve, reject (§7.3–7.5).
                Renders nothing when the role/status combo has no
                permission (§10). */}
            <WorkflowPanel
              invoice={invoice}
              user={user}
              users={users}
              busy={busy}
              onAssign={(reviewerId, approverId) =>
                act(
                  "assign",
                  { reviewer_id: reviewerId, approver_id: approverId },
                  "Assigned for review."
                )
              }
              onReassign={(reviewerId, approverId) =>
                act(
                  "reassign",
                  { reviewer_id: reviewerId, approver_id: approverId },
                  "Reassigned."
                )
              }
              onReview={(approverId) =>
                act(
                  "mark-reviewed",
                  { approver_id: approverId },
                  "Marked reviewed and sent for approval."
                )
              }
              onApprove={() =>
                act(
                  "approve",
                  undefined,
                  "Approved. It will go up in the next BuilderTrend session."
                )
              }
              onReject={(reason) =>
                act("reject", { reason }, "Sent back to the reviewer.")
              }
            />

            {/* Triage actions. Renders nothing when the role/status
                combination has no permission (§10). */}
            {canTriage && (
              <TriagePanel
                invoice={invoice}
                busy={busy}
                onAct={act}
              />
            )}

            {/* Line items */}
            {invoice.lines.length > 0 && (
              <div className="glass section-card" style={{ marginBottom: 18 }}>
                <div className="section-header">
                  <div className="section-title">
                    Line items ({invoice.lines.length})
                  </div>
                </div>
                <div className="table-scroll">
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>Ticket</th>
                        <th>Mix no.</th>
                        <th>Description</th>
                        <th className="num">Qty</th>
                        <th>UOM</th>
                        <th className="num">Gross</th>
                      </tr>
                    </thead>
                    <tbody>
                      {invoice.lines.map((l) => (
                        <tr key={l.id}>
                          <td className="mono">{l.ticket_no || "—"}</td>
                          <td className="mono">{l.prod_num || "—"}</td>
                          <td>{l.description || "—"}</td>
                          <td className="num">{l.qty ?? "—"}</td>
                          <td>{l.uom || "—"}</td>
                          <td className="num">{fmtMoney(l.gross)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {/* Audit trail */}
            <div className="glass section-card">
              <div className="section-header">
                <div className="section-title">History</div>
              </div>
              {invoice.audit.length === 0 ? (
                <div style={{ fontSize: 13, color: "var(--text-faint)" }}>
                  Nothing recorded yet.
                </div>
              ) : (
                invoice.audit.map((a) => (
                  <div key={a.id} className="audit-row">
                    <span className="audit-when">{fmtDateTime(a.at)}</span>
                    <span className="audit-what">
                      {describeAudit(a.action, a.from_status, a.to_status)}
                      {a.diff && Object.keys(a.diff).length > 0 && (
                        <span className="cell-sub">
                          {summarizeDiff(a.diff)}
                        </span>
                      )}
                    </span>
                    <span className="audit-who">{actorLabel(a)}</span>
                  </div>
                ))
              )}
            </div>
          </div>
        </div>
      </div>
    </>
  );
}

// ─── Status banner (§10) ──────────────────────────────────────────────

function StatusBanner({ invoice }: { invoice: InvoiceDetail }) {
  const tone =
    invoice.status === "flagged"
      ? "red"
      : invoice.status === "void"
        ? "muted"
        : invoice.rejected_at && invoice.status === "assigned"
          ? "amber"
          : ["uploaded", "filed"].includes(invoice.status)
            ? "green"
            : "blue";

  let body: React.ReactNode;
  if (invoice.status === "flagged") {
    body = (
      <>
        {invoice.flag_detail || "No detail recorded."}
        {invoice.duplicate_of && (
          <div style={{ marginTop: 8 }}>
            Possibly the same as{" "}
            <Link
              href={`/invoices/${invoice.duplicate_of.id}`}
              className="dash-open-link"
            >
              {invoice.duplicate_of.invoice_no || "that invoice"}
            </Link>{" "}
            ({fmtMoney(invoice.duplicate_of.amount)} on{" "}
            {fmtDate(invoice.duplicate_of.invoice_date)}).
          </div>
        )}
      </>
    );
  } else if (invoice.status === "void") {
    body = invoice.void_reason || "Voided.";
  } else if (invoice.reject_reason && invoice.status === "assigned") {
    body = (
      <>
        Sent back by the approver: {invoice.reject_reason}
      </>
    );
  } else if (invoice.status === "suggested") {
    body =
      "Read and coded by the AI. Waiting for accounting to assign a reviewer.";
  } else if (invoice.status === "ingested") {
    body =
      "Downloaded from Drive but not yet read. The AI runs on ingest, so this usually means the extraction has not completed.";
  } else if (invoice.status === "assigned") {
    body = `With ${invoice.reviewer_name || "the reviewer"} for review.`;
  } else if (invoice.status === "pending_approval") {
    body = `Reviewed. Awaiting approval from ${
      invoice.approver_name || "an approver"
    }.`;
  } else if (invoice.status === "approved") {
    body =
      "Approved and ready for the next Chrome upload session. Nothing else is needed here.";
  } else if (invoice.status === "uploaded") {
    // `uploaded` and not `filed` means the bill is real and the Drive half is
    // not done. Saying which half is outstanding matters: the recovery for a
    // filing failure is a Drive folder, never a second BuilderTrend save.
    body = (
      <>
        Entered in BuilderTrend
        {invoice.bt_bill_id ? ` as bill ${invoice.bt_bill_id}` : ""}.{" "}
        {invoice.filing_error ? (
          <>
            The Drive copy did not file: {invoice.filing_error} The bill itself
            is fine — retry the filing from{" "}
            <Link href="/uploads" className="dash-open-link">
              Uploads
            </Link>
            .
          </>
        ) : (
          "Filing still pending."
        )}
      </>
    );
  } else {
    body = (
      <>
        Filed{invoice.filed_path ? ` to ${invoice.filed_path}` : ""}.
        {!invoice.original_archived && (
          <> The Drive original is still in the intake folder.</>
        )}
        {invoice.filing_warnings.map((w) => (
          <div style={{ marginTop: 6 }} key={w}>
            {w}
          </div>
        ))}
      </>
    );
  }

  return (
    <div className={`status-banner status-banner-${tone}`}>
      <div className="status-banner-head">
        <span className={`pill ${statusPillClass(invoice.status)}`}>
          {INVOICE_STATUS_LABELS[invoice.status]}
        </span>
        <span className="status-banner-title">
          {invoice.status === "flagged" && invoice.flag_code
            ? FLAG_LABELS[invoice.flag_code as FlagCode]
            : INVOICE_STATUS_LABELS[invoice.status]}
        </span>
        {invoice.age_days !== null && (
          <span style={{ fontSize: 12.5, color: "var(--text-faint)" }}>
            {invoice.age_days} day{invoice.age_days === 1 ? "" : "s"} old
          </span>
        )}
      </div>
      <div className="status-banner-body">{body}</div>
    </div>
  );
}

// ─── Triage panel ─────────────────────────────────────────────────────

function TriagePanel({
  invoice,
  busy,
  onAct,
}: {
  invoice: InvoiceDetail;
  busy: boolean;
  onAct: (path: string, body?: unknown, message?: string) => void;
}) {
  const [voidReason, setVoidReason] = useState("");
  const [showVoid, setShowVoid] = useState(false);

  const inBuilderTrend = ["uploaded", "filed"].includes(invoice.status);
  const isVoid = invoice.status === "void";
  const canRetry = !["approved", "uploaded", "filed", "void"].includes(
    invoice.status
  );

  if (isVoid) return null;

  return (
    <div className="glass section-card" style={{ marginBottom: 18 }}>
      <div className="section-header">
        <div className="section-title">Triage</div>
      </div>

      <div
        style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 4 }}
      >
        {invoice.status === "flagged" && (
          <button
            className="btn"
            disabled={busy}
            onClick={() =>
              onAct(
                "unflag",
                undefined,
                "Flag cleared. The invoice went back to where it was."
              )
            }
          >
            Clear flag
          </button>
        )}

        {invoice.flag_code === "possible_duplicate" && (
          <button
            className="btn"
            disabled={busy}
            onClick={() =>
              onAct(
                "not-duplicate",
                { note: "Confirmed a distinct invoice." },
                "Marked as not a duplicate."
              )
            }
          >
            Not a duplicate
          </button>
        )}

        {canRetry && (
          <button
            className="btn"
            disabled={busy}
            onClick={() =>
              onAct("retry-suggestion", undefined, "The AI re-read the invoice.")
            }
            title="Re-runs extraction. Any project or vendor you set is passed to the model as settled."
          >
            Re-run the AI
          </button>
        )}

        {invoice.status !== "flagged" && (
          <button
            className="btn"
            disabled={busy}
            onClick={() =>
              onAct(
                "flag",
                {
                  code: "stop_and_ask",
                  detail: "Flagged manually for review.",
                },
                "Flagged."
              )
            }
          >
            Flag for triage
          </button>
        )}

        {!inBuilderTrend && (
          <button
            className="btn dash-btn-red"
            disabled={busy}
            onClick={() => setShowVoid((v) => !v)}
          >
            {showVoid ? "Cancel" : "Void"}
          </button>
        )}
      </div>

      {inBuilderTrend && (
        <div className="callout callout-info" style={{ marginTop: 12 }}>
          <div className="callout-title">Already in BuilderTrend</div>
          This bill exists in BuilderTrend, so it cannot be voided here — void
          it there first, or the two records will disagree.
        </div>
      )}

      {showVoid && (
        <div style={{ marginTop: 14 }}>
          <div className="form-row">
            <label className="form-label" htmlFor="void-reason">
              Why is this being voided?
            </label>
            <input
              id="void-reason"
              className="input"
              value={voidReason}
              onChange={(e) => setVoidReason(e.target.value)}
              placeholder="Duplicate of 8461, already entered by hand"
            />
            <div className="form-help">
              Required. The reason is kept on the record and in the history.
            </div>
          </div>
          <button
            className="btn dash-btn-red"
            disabled={busy || !voidReason.trim()}
            onClick={() => {
              onAct("void", { reason: voidReason.trim() }, "Voided.");
              setShowVoid(false);
              setVoidReason("");
            }}
          >
            Void this invoice
          </button>
        </div>
      )}
    </div>
  );
}

// ─── Small pieces ─────────────────────────────────────────────────────

function Field({
  label,
  value,
  canEdit,
  busy,
  onSave,
  help,
  type = "text",
  numeric = false,
}: {
  label: string;
  value: string;
  canEdit: boolean;
  busy: boolean;
  onSave: (v: string) => void;
  help?: string;
  type?: string;
  numeric?: boolean;
}) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);

  const id = label.toLowerCase().replace(/[^a-z0-9]+/g, "-");

  if (!canEdit) {
    return (
      <div className="form-row">
        <label className="form-label">{label}</label>
        <div style={{ fontSize: 14 }}>{value || "—"}</div>
        {help && <div className="form-help">{help}</div>}
      </div>
    );
  }

  return (
    <div className="form-row">
      <label className="form-label" htmlFor={id}>
        {label}
      </label>
      <input
        id={id}
        className="input"
        type={type}
        value={draft}
        disabled={busy}
        inputMode={numeric ? "decimal" : undefined}
        onChange={(e) =>
          setDraft(
            numeric ? e.target.value.replace(/[^0-9.-]/g, "") : e.target.value
          )
        }
        onBlur={() => {
          if (draft !== value) onSave(draft);
        }}
      />
      {help && <div className="form-help">{help}</div>}
    </div>
  );
}

function describeAudit(
  action: string,
  from: string | null,
  to: string | null
): string {
  const phrases: Record<string, string> = {
    ingested: "Downloaded from Drive",
    suggested: "Read and coded by the AI",
    flagged: "Flagged for triage",
    unflagged: "Flag cleared",
    marked_not_duplicate: "Marked as not a duplicate",
    voided: "Voided",
    updated: "Fields corrected",
    costs_updated: "Cost coding changed",
    assigned: "Assigned for review",
    reviewed: "Marked reviewed",
    approved: "Approved",
    rejected: "Sent back to the reviewer",
    uploaded: "Entered in BuilderTrend",
    filed: "Filed on Drive",
  };
  const base = phrases[action] ?? action.replace(/_/g, " ");
  if (from && to && from !== to) return `${base} (${from} → ${to})`;
  return base;
}

function summarizeDiff(diff: Record<string, unknown>): string {
  return Object.entries(diff)
    .slice(0, 4)
    .map(([key, value]) => {
      if (
        value &&
        typeof value === "object" &&
        "from" in (value as object) &&
        "to" in (value as object)
      ) {
        const v = value as { from: unknown; to: unknown };
        return `${key}: ${v.from ?? "blank"} → ${v.to ?? "blank"}`;
      }
      return `${key}: ${String(value)}`;
    })
    .join(" · ");
}
