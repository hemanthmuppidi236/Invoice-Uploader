"use client";

/**
 * Uploads — `/uploads` (prompt §10)
 *
 * "The approved-not-uploaded queue as Linda sees it before starting a Chrome
 * session, plus the last session's results."
 *
 * The app never drives the browser (§7.6). Linda opens the chat herself, and
 * this screen is the thing she reads first: it renders the exact payload the
 * Chrome session will read, field for field, rather than a friendlier
 * summary. A preview that re-derived the values could agree with itself and
 * still disagree with the API, which is the one failure this screen exists to
 * prevent.
 *
 * Three sections, in the order the work happens:
 *
 *   1. **Blocked** — approved but missing something BuilderTrend needs. Fixed
 *      here, before a session starts, because a session that hits one of
 *      these has to stop and ask (SOP §7).
 *   2. **Ready** — one card per invoice, with its warnings.
 *   3. **In BuilderTrend, not yet filed** and **recent results** — what the
 *      last session did, and the retry for the Drive half.
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api, formatApiError } from "@/lib/api";
import { ErrorBanner } from "@/components/ErrorBanner";
import { useCurrentUser } from "@/lib/useCurrentUser";
import {
  CHROME_SESSION_PROMPT,
  fmtDate,
  fmtDateTime,
  fmtMoney,
  hasRole,
  INVOICE_STATUS_LABELS,
  statusPillClass,
  type Invoice,
  type MarkUploadedResult,
  type UploadQueue,
  type UploadQueueItem,
} from "@/lib/types";

export default function UploadsPage() {
  const { user, loading: userLoading } = useCurrentUser();
  const [data, setData] = useState<UploadQueue | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const canRun = hasRole(user, "admin", "accountant");

  const load = useCallback(async () => {
    try {
      setData(await api.get<UploadQueue>("/invoices/upload-queue"));
      setError(null);
    } catch (e) {
      setError(formatApiError(e));
      setData(null);
    }
  }, []);

  useEffect(() => {
    if (canRun) load();
  }, [canRun, load]);

  async function retryFiling(invoiceId: string) {
    setBusyId(invoiceId);
    setError(null);
    setNotice(null);
    try {
      const result = await api.post<MarkUploadedResult>(
        `/invoices/${invoiceId}/mark-filed`,
        {}
      );
      setNotice(
        result.filing.filed_path
          ? `Filed to ${result.filing.filed_path}.`
          : "Filed."
      );
      await load();
    } catch (e) {
      setError(formatApiError(e));
    } finally {
      setBusyId(null);
    }
  }

  async function copyPrompt() {
    try {
      await navigator.clipboard.writeText(CHROME_SESSION_PROMPT);
      setCopied(true);
      setTimeout(() => setCopied(false), 2500);
    } catch {
      // Clipboard access can be refused; the text is on screen to select.
      setCopied(false);
    }
  }

  if (!userLoading && !canRun) {
    return (
      <>
        <PageHeader />
        <div className="page-content">
          <div className="glass section-card">
            <div className="empty-state">
              <div className="empty-state-title">Accounting runs the uploads</div>
              <div className="empty-state-desc">
                Only accounting starts a BuilderTrend session, so this queue is
                theirs. Approved invoices are visible on{" "}
                <Link href="/invoices?status=approved" className="dash-open-link">
                  the invoice list
                </Link>{" "}
                if you want to check where one got to.
              </div>
            </div>
          </div>
        </div>
      </>
    );
  }

  return (
    <>
      <PageHeader onRefresh={load} />

      <div className="page-content">
        {error && <ErrorBanner message={error} onDismiss={() => setError(null)} />}
        {notice && (
          <div className="callout callout-ok">
            <div className="callout-title">Done</div>
            {notice}
          </div>
        )}

        {/* How a session starts. The app deliberately has no button for this
            — §7.6: "The app never triggers the browser. Linda always starts
            the session." */}
        <div className="glass section-card" style={{ marginBottom: 20 }}>
          <div className="section-header">
            <div className="section-title">Starting a session</div>
            {data && (
              <span className="chip">
                Queue built {fmtDateTime(data.generated_at)}
              </span>
            )}
          </div>
          <div style={{ fontSize: 13.5, color: "var(--text-muted)" }}>
            Open a chat in the <strong>Invoice Upload on Builder Trend</strong>{" "}
            project with Chrome signed in to BuilderTrend, and paste this. The
            app does not open the browser for you, on purpose — a session
            starts because a person decided to start it.
          </div>
          <div className="session-prompt">{CHROME_SESSION_PROMPT}</div>
          <button className="btn" onClick={copyPrompt}>
            {copied ? "Copied" : "Copy the prompt"}
          </button>

          {data?.notes.map((note) => (
            <div className="callout callout-info" key={note}>
              {note}
            </div>
          ))}
        </div>

        {!data && !error && (
          <div className="glass section-card">
            <div style={{ color: "var(--text-muted)" }}>Loading the queue…</div>
          </div>
        )}

        {data && data.blocked.length > 0 && (
          <div className="glass section-card" style={{ marginBottom: 20 }}>
            <div className="section-header">
              <div className="section-title">Fix before the session starts</div>
              <span className="pill pill-red">{data.blocked.length}</span>
            </div>
            <div className="callout callout-block">
              <div className="callout-title">
                Approved, but BuilderTrend cannot be given a value
              </div>
              These are already through review and approval, so the numbers are
              trusted — what is missing is reference data. The session would
              have to stop and ask on each of them (SOP §7), so it is cheaper
              to fix them now.
            </div>
            {data.blocked.map((item) => (
              <UploadCard key={item.invoice_id} item={item} blocked />
            ))}
          </div>
        )}

        {data && (
          <div className="glass section-card" style={{ marginBottom: 20 }}>
            <div className="section-header">
              <div className="section-title">Ready to upload</div>
              <span className="pill pill-green">{data.queue.length}</span>
            </div>

            {data.queue.length === 0 ? (
              <div className="empty-state">
                <div className="empty-state-title">Nothing waiting</div>
                <div className="empty-state-desc">
                  Every approved invoice is already in BuilderTrend. Invoices
                  arrive here once an approver signs off, so an empty queue
                  means the review side is caught up too.
                </div>
              </div>
            ) : (
              <>
                <div style={{ fontSize: 13, color: "var(--text-muted)", marginBottom: 12 }}>
                  These are the values the session will type, not a summary of
                  them. Checking a card against the PDF is checking the actual
                  entry.
                </div>
                {data.queue.map((item) => (
                  <UploadCard key={item.invoice_id} item={item} />
                ))}
              </>
            )}
          </div>
        )}

        {data && data.awaiting_filing.length > 0 && (
          <div className="glass section-card" style={{ marginBottom: 20 }}>
            <div className="section-header">
              <div className="section-title">In BuilderTrend, not yet filed</div>
              <span className="pill pill-amber">
                {data.awaiting_filing.length}
              </span>
            </div>
            <div className="callout callout-warn">
              <div className="callout-title">The bill saved; the Drive copy did not</div>
              The bookkeeping is done — these are real bills in BuilderTrend.
              What is left is the §7.7 filing: a copy into{" "}
              <span className="font-mono">BT Invoices/[Project]/[Vendor]/</span>{" "}
              and the original moved to <span className="font-mono">Uploaded/</span>.
              Nothing is ever deleted, so a retry is safe. If the reason is a
              missing folder, create it in Drive first — the app will not.
            </div>
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Invoice</th>
                    <th>Bill</th>
                    <th>Why it did not file</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {data.awaiting_filing.map((inv) => (
                    <tr key={inv.id}>
                      <td>
                        <Link href={`/invoices/${inv.id}`} className="dash-open-link">
                          {inv.invoice_no || inv.source_filename || inv.id}
                        </Link>
                        <span className="cell-sub">
                          {inv.vendor_name || "unknown vendor"}
                          {inv.project_name && ` · ${inv.project_name}`}
                          {inv.amount !== null && ` · ${fmtMoney(inv.amount)}`}
                        </span>
                      </td>
                      <td className="mono">{inv.bt_bill_id || "—"}</td>
                      <td style={{ maxWidth: 420 }}>
                        {inv.filing_error || "Not attempted yet."}
                      </td>
                      <td>
                        <button
                          className="btn"
                          style={{ padding: "4px 10px", fontSize: 12 }}
                          disabled={busyId === inv.id}
                          onClick={() => retryFiling(inv.id)}
                        >
                          {busyId === inv.id ? "Filing…" : "Retry filing"}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {data && (
          <div className="glass section-card">
            <div className="section-header">
              <div className="section-title">Recent sessions</div>
              <span className="chip">{data.recently_uploaded.length} bills</span>
            </div>
            {data.recently_uploaded.length === 0 ? (
              <div className="empty-state">
                <div className="empty-state-title">No uploads recorded yet</div>
                <div className="empty-state-desc">
                  A row appears here the moment a session reports a verified
                  save, with the BuilderTrend bill id it reported.
                </div>
              </div>
            ) : (
              <div className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Invoice</th>
                      <th>Uploaded</th>
                      <th>BT bill</th>
                      <th>Filed to</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.recently_uploaded.map((inv) => (
                      <tr key={inv.id}>
                        <td>
                          <Link href={`/invoices/${inv.id}`} className="dash-open-link">
                            {inv.invoice_no || inv.source_filename || inv.id}
                          </Link>
                          <span className="cell-sub">
                            {inv.vendor_name || "unknown vendor"}
                            {inv.project_name && ` · ${inv.project_name}`}
                            {inv.amount !== null && ` · ${fmtMoney(inv.amount)}`}
                          </span>
                        </td>
                        <td>{fmtDateTime(inv.uploaded_at)}</td>
                        <td className="mono">{inv.bt_bill_id || "—"}</td>
                        <td style={{ maxWidth: 320 }}>
                          <span className="font-mono" style={{ fontSize: 12 }}>
                            {inv.filed_path || "—"}
                          </span>
                          {inv.filed_path && !inv.original_archived && (
                            <span className="cell-sub">
                              Original still in the intake folder
                            </span>
                          )}
                          {inv.filing_warnings.map((w) => (
                            <span className="cell-sub" key={w}>
                              {w}
                            </span>
                          ))}
                        </td>
                        <td>
                          <span className={`pill ${statusPillClass(inv.status)}`}>
                            {INVOICE_STATUS_LABELS[inv.status]}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </div>
    </>
  );
}

function PageHeader({ onRefresh }: { onRefresh?: () => void }) {
  return (
    <div className="page-header">
      <div className="page-title-block">
        <div className="page-eyebrow">Ferrocrete Builders, Inc.</div>
        <h1 className="page-title">Uploads</h1>
        <div className="page-meta">
          What the Chrome session will enter in BuilderTrend, checked before it
          starts
        </div>
      </div>
      <div className="page-actions">
        {onRefresh && (
          <button className="btn btn-ghost" onClick={onRefresh}>
            Refresh
          </button>
        )}
        <Link href="/invoices?status=approved" className="btn btn-ghost">
          All approved
        </Link>
      </div>
    </div>
  );
}

/**
 * One invoice, as the BuilderTrend bill form.
 *
 * Field labels are BuilderTrend's, not the app's — "Pay to", "Bill #",
 * "Title" — so the card can be read straight down the form. Calling them
 * "vendor" and "invoice number" here would mean translating twice.
 */
function UploadCard({
  item,
  blocked = false,
}: {
  item: UploadQueueItem;
  blocked?: boolean;
}) {
  const quirks = Object.entries(item.quirks ?? {});
  const costTotal = item.costs.reduce((sum, c) => sum + c.amount, 0);

  return (
    <div className={`upload-card${blocked ? " upload-card-blocked" : ""}`}>
      <div className="upload-card-head">
        <div>
          <div className="upload-card-title">
            {item.pay_to || "No BuilderTrend vendor"} · {fmtMoney(item.amount)}
          </div>
          <div className="upload-card-sub">
            {item.project_no ? `${item.project_no} — ` : ""}
            {item.project_name || "No project"}
            {item.invoice_no && ` · invoice ${item.invoice_no}`}
            {item.age_days !== null &&
              ` · approved from an invoice ${item.age_days} day${
                item.age_days === 1 ? "" : "s"
              } old`}
          </div>
        </div>
        <div className="upload-card-actions">
          <Link
            href={`/invoices/${item.invoice_id}`}
            className="btn btn-ghost"
            style={{ padding: "4px 10px", fontSize: 12 }}
          >
            Open invoice
          </Link>
          {item.pdf_url && (
            <a
              href={item.pdf_url}
              target="_blank"
              rel="noreferrer"
              className="btn btn-ghost"
              style={{ padding: "4px 10px", fontSize: 12 }}
            >
              PDF
            </a>
          )}
          {item.bill_url && (
            <a
              href={item.bill_url}
              target="_blank"
              rel="noreferrer"
              className="btn btn-ghost"
              style={{ padding: "4px 10px", fontSize: 12 }}
            >
              New bill in BT
            </a>
          )}
        </div>
      </div>

      {blocked && (
        <div className="callout callout-block">
          <div className="callout-title">Do not enter this one yet</div>
          <ul>
            {item.blockers.map((b) => (
              <li key={b}>{b}</li>
            ))}
          </ul>
        </div>
      )}

      {item.warnings.length > 0 && (
        <div className="callout callout-warn">
          <div className="callout-title">Read first</div>
          <ul>
            {item.warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </div>
      )}

      {quirks.length > 0 && (
        <div className="callout callout-info">
          <div className="callout-title">Quirks recorded for this job</div>
          <ul>
            {quirks.map(([key, value]) => (
              <li key={key}>
                <strong>{key}:</strong> {String(value)}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="upload-fields">
        <div>
          <Field label="Title" value={item.bill_title} hint="base cost code" />
          <Field label="Bill #" value={item.bill_no} hint="last 4 digits" />
          <Field label="Pay to" value={item.pay_to} />
        </div>
        <div>
          <Field label="Invoice date" value={fmtDate(item.invoice_date)} />
          <Field
            label="Due date"
            value={fmtDate(item.due_date)}
            hint="end of the month after"
          />
          <Field
            label="Unit cost"
            value={fmtMoney(item.amount)}
            hint={item.is_credit ? "credit memo, enter negative" : "qty 1"}
          />
        </div>
      </div>

      <div className="preview-divider" />

      <div style={{ fontSize: 12.5, color: "var(--text-muted)", marginBottom: 6 }}>
        Costs rows — one per cost code, Title left blank
      </div>
      {item.costs.length === 0 ? (
        <div style={{ fontSize: 13, color: "var(--status-red)" }}>
          No cost rows.
        </div>
      ) : (
        <>
          {item.costs.map((c) => (
            <div className="preview-line muted" key={c.cost_code}>
              <span className="preview-line-label">{c.cost_code}</span>
              <span className="preview-line-value">{fmtMoney(c.amount)}</span>
            </div>
          ))}
          <div className="cost-total">
            <span>Total</span>
            <span
              className={
                item.amount !== null && costTotal === item.amount
                  ? "cost-total-ok"
                  : undefined
              }
            >
              {fmtMoney(costTotal)}
            </span>
          </div>
        </>
      )}

      {(item.project_notes || item.vendor_notes) && (
        <div style={{ marginTop: 10, fontSize: 12.5, color: "var(--text-muted)" }}>
          {item.project_notes && <div>Project note: {item.project_notes}</div>}
          {item.vendor_notes && <div>Vendor note: {item.vendor_notes}</div>}
        </div>
      )}
    </div>
  );
}

function Field({
  label,
  value,
  hint,
}: {
  label: string;
  value: string | null;
  hint?: string;
}) {
  return (
    <div className="preview-line">
      <span className="preview-line-label">
        {label}
        {hint && (
          <span style={{ color: "var(--text-faint)", fontSize: 12 }}> · {hint}</span>
        )}
      </span>
      <span className="preview-line-value">{value || "—"}</span>
    </div>
  );
}
