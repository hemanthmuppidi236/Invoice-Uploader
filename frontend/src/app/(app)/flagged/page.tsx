"use client";

/**
 * Flagged triage — `/flagged` (prompt §10)
 *
 * Linda's queue. Grouped by reason rather than listed flat, because the
 * reasons need different fixes: every "unknown vendor" is one missing mapping
 * away from resolving, while every "possible duplicate" needs a judgement
 * call. Grouping lets a whole class get cleared in one pass.
 *
 * Each row carries the quick actions the spec names — set project, set
 * vendor, mark not a duplicate, retry the AI, void — inline, so the common
 * fix does not need a trip to the detail screen.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { api, formatApiError } from "@/lib/api";
import { ErrorBanner } from "@/components/ErrorBanner";
import { useCurrentUser } from "@/lib/useCurrentUser";
import {
  FLAG_LABELS,
  fmtDate,
  fmtMoney,
  hasRole,
  type FlagCode,
  type Invoice,
  type InvoiceDashboard,
  type Project,
  type Vendor,
} from "@/lib/types";

/**
 * What to do about each reason. Written for the person triaging, not as a
 * restatement of the code name — "unknown vendor" is obvious, "add the
 * letterhead-to-BuilderTrend mapping under Admin" is the actual next step.
 */
const FLAG_GUIDANCE: Record<FlagCode, string> = {
  unknown_vendor:
    "The letterhead name is not in the vendor list. Add the mapping under Admin → Vendors using the name BuilderTrend already uses, then re-run the AI. Never create a new vendor in BuilderTrend for this.",
  unknown_project:
    "The job could not be matched. Set the project below if you recognise it, then re-run the AI so the cost code gets scored against that project's mix designs.",
  new_project:
    "This belongs to a current job, which goes through a separate workflow. Leave it here; do not enter it in BuilderTrend from this app.",
  project_not_onboarded:
    "The project exists but has no project engineer, or no confirmed mix design. Finish onboarding it, then re-run the AI.",
  possible_duplicate:
    "An invoice with the same vendor and number, or the same vendor, amount, and date, already exists. Open both. If they are genuinely different bills, mark not a duplicate; if not, void this one.",
  unreadable:
    "A field could not be read off the PDF. Open it and fill in what is missing by hand, or re-scan the document at a higher resolution and drop it back in the Drive folder.",
  yard: "A White Cap yard invoice. These are never entered in BuilderTrend. Void it to clear the queue.",
  ai_error:
    "The extraction call failed. The detail below carries the error. Re-run the AI — if it fails the same way twice, the PDF is usually the problem.",
  stop_and_ask:
    "Something needed a person. The detail says what.",
  other: "Flagged manually. The detail says why.",
};

/** Reasons where picking the project is the fix, so the select is shown. */
const NEEDS_PROJECT: FlagCode[] = ["unknown_project", "project_not_onboarded"];
/** Reasons where picking the vendor is the fix. */
const NEEDS_VENDOR: FlagCode[] = ["unknown_vendor"];

export default function FlaggedPage() {
  const { user } = useCurrentUser();
  const [rows, setRows] = useState<Invoice[] | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [vendors, setVendors] = useState<Vendor[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const canTriage = hasRole(user, "admin", "accountant");

  const load = useCallback(async () => {
    try {
      const data = await api.get<InvoiceDashboard>("/invoices?status=flagged");
      setRows(data.invoices);
    } catch (e) {
      setError(formatApiError(e));
      setRows([]);
    }
  }, []);

  useEffect(() => {
    load();
    Promise.all([
      api.get<Project[]>("/projects"),
      api.get<Vendor[]>("/admin/vendors?active_only=true"),
    ])
      .then(([p, v]) => {
        setProjects(p);
        setVendors(v);
      })
      .catch((e) => setError(formatApiError(e)));
  }, [load]);

  const grouped = useMemo(() => {
    const groups = new Map<FlagCode, Invoice[]>();
    for (const row of rows ?? []) {
      const code = (row.flag_code ?? "other") as FlagCode;
      groups.set(code, [...(groups.get(code) ?? []), row]);
    }
    // Biggest bucket first: the reason with the most invoices is usually one
    // fix away from clearing several at once.
    return [...groups.entries()].sort((a, b) => b[1].length - a[1].length);
  }, [rows]);

  async function run(
    invoiceId: string,
    fn: () => Promise<unknown>,
    message: string
  ) {
    setBusyId(invoiceId);
    setError(null);
    setNotice(null);
    try {
      await fn();
      setNotice(message);
      await load();
    } catch (e) {
      setError(formatApiError(e));
    } finally {
      setBusyId(null);
    }
  }

  return (
    <>
      <div className="page-header">
        <div className="page-title-block">
          <div className="page-eyebrow">Ferrocrete Builders, Inc.</div>
          <h1 className="page-title">Flagged</h1>
          <div className="page-meta">
            Anything intake or the AI would not route on its own. Nothing here
            is lost — it is waiting on a decision.
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

        {rows === null && (
          <div className="glass section-card">
            <div style={{ color: "var(--text-muted)" }}>Loading…</div>
          </div>
        )}

        {rows !== null && rows.length === 0 && (
          <div className="glass section-card">
            <div className="empty-state">
              <div className="empty-state-title">Nothing flagged</div>
              <div className="empty-state-desc">
                Every invoice intake has seen either routed for review or is
                already through. This queue filling up is normal — an unknown
                vendor or an unmatched job is a missing piece of reference
                data, not a failure.
              </div>
            </div>
          </div>
        )}

        {grouped.map(([code, invoices]) => (
          <div
            key={code}
            className="glass section-card"
            style={{ marginBottom: 20 }}
          >
            <div className="section-header">
              <div className="section-title">{FLAG_LABELS[code]}</div>
              <span className="pill pill-red">{invoices.length}</span>
            </div>

            <div className="callout callout-info">
              <div className="callout-title">How to clear these</div>
              {FLAG_GUIDANCE[code]}
            </div>

            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Invoice</th>
                    <th>Detail</th>
                    {NEEDS_PROJECT.includes(code) && <th>Set project</th>}
                    {NEEDS_VENDOR.includes(code) && <th>Set vendor</th>}
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {invoices.map((inv) => (
                    <tr key={inv.id}>
                      <td>
                        <Link
                          href={`/invoices/${inv.id}`}
                          className="dash-open-link"
                        >
                          {inv.invoice_no ||
                            inv.source_filename ||
                            "Untitled invoice"}
                        </Link>
                        <span className="cell-sub">
                          {inv.vendor_name || "unknown vendor"}
                          {inv.amount !== null && ` · ${fmtMoney(inv.amount)}`}
                          {inv.invoice_date && ` · ${fmtDate(inv.invoice_date)}`}
                        </span>
                        <span className="cell-sub">
                          {inv.age_days !== null &&
                            `${inv.age_days} day${
                              inv.age_days === 1 ? "" : "s"
                            } in this queue`}
                        </span>
                      </td>
                      <td style={{ maxWidth: 380 }}>
                        {inv.flag_detail || "—"}
                        {inv.duplicate_of_invoice_id && (
                          <span className="cell-sub">
                            <Link
                              href={`/invoices/${inv.duplicate_of_invoice_id}`}
                              className="dash-open-link"
                            >
                              Open the other invoice
                            </Link>
                          </span>
                        )}
                      </td>

                      {NEEDS_PROJECT.includes(code) && (
                        <td>
                          <select
                            className="cell-select"
                            value={inv.project_id ?? ""}
                            disabled={!canTriage || busyId === inv.id}
                            onChange={(e) =>
                              run(
                                inv.id,
                                () =>
                                  api.patch(`/invoices/${inv.id}`, {
                                    project_id: e.target.value || null,
                                  }),
                                "Project set. Re-run the AI to re-score the cost code."
                              )
                            }
                          >
                            <option value="">Not matched</option>
                            {projects.map((p) => (
                              <option key={p.id} value={p.id}>
                                {p.project_no} — {p.name}
                              </option>
                            ))}
                          </select>
                        </td>
                      )}

                      {NEEDS_VENDOR.includes(code) && (
                        <td>
                          <select
                            className="cell-select"
                            value={inv.vendor_id ?? ""}
                            disabled={!canTriage || busyId === inv.id}
                            onChange={(e) =>
                              run(
                                inv.id,
                                () =>
                                  api.patch(`/invoices/${inv.id}`, {
                                    vendor_id: e.target.value || null,
                                  }),
                                "Vendor set. Re-run the AI to finish the coding."
                              )
                            }
                          >
                            <option value="">Not matched</option>
                            {vendors.map((v) => (
                              <option key={v.id} value={v.id}>
                                {v.invoice_name} → {v.bt_name}
                              </option>
                            ))}
                          </select>
                        </td>
                      )}

                      <td>
                        {canTriage ? (
                          <RowActions
                            invoice={inv}
                            code={code}
                            busy={busyId === inv.id}
                            onRun={run}
                          />
                        ) : (
                          <span
                            style={{ fontSize: 12, color: "var(--text-faint)" }}
                          >
                            Accounting clears these
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ))}
      </div>
    </>
  );
}

function RowActions({
  invoice,
  code,
  busy,
  onRun,
}: {
  invoice: Invoice;
  code: FlagCode;
  busy: boolean;
  onRun: (id: string, fn: () => Promise<unknown>, message: string) => void;
}) {
  const [voiding, setVoiding] = useState(false);
  const [reason, setReason] = useState(
    code === "yard" ? "White Cap yard invoice — never entered per SOP §5." : ""
  );

  const smallBtn = { padding: "4px 10px", fontSize: 12 } as const;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        {code === "possible_duplicate" && (
          <button
            className="btn"
            style={smallBtn}
            disabled={busy}
            onClick={() =>
              onRun(
                invoice.id,
                () =>
                  api.post(`/invoices/${invoice.id}/not-duplicate`, {
                    note: "Confirmed a distinct invoice.",
                  }),
                "Marked as not a duplicate."
              )
            }
          >
            Not a duplicate
          </button>
        )}

        {code !== "new_project" && code !== "yard" && (
          <button
            className="btn"
            style={smallBtn}
            disabled={busy}
            onClick={() =>
              onRun(
                invoice.id,
                () => api.post(`/invoices/${invoice.id}/retry-suggestion`),
                "The AI re-read the invoice."
              )
            }
          >
            Re-run the AI
          </button>
        )}

        <button
          className="btn"
          style={smallBtn}
          disabled={busy}
          onClick={() =>
            onRun(
              invoice.id,
              () => api.post(`/invoices/${invoice.id}/unflag`),
              "Flag cleared."
            )
          }
          title="Clears the flag and restores the state the invoice was in before it"
        >
          Clear flag
        </button>

        <button
          className="btn dash-btn-red"
          style={smallBtn}
          disabled={busy}
          onClick={() => setVoiding((v) => !v)}
        >
          {voiding ? "Cancel" : "Void"}
        </button>
      </div>

      {voiding && (
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          <input
            className="cell-select"
            style={{ minWidth: 200 }}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Reason, kept on the record"
          />
          <button
            className="btn dash-btn-red"
            style={smallBtn}
            disabled={busy || !reason.trim()}
            onClick={() => {
              onRun(
                invoice.id,
                () =>
                  api.post(`/invoices/${invoice.id}/void`, {
                    reason: reason.trim(),
                  }),
                "Voided."
              );
              setVoiding(false);
            }}
          >
            Confirm void
          </button>
        </div>
      )}
    </div>
  );
}
