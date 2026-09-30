"use client";

/**
 * Invoices dashboard — `/invoices` (prompt §10)
 *
 * The default landing. "Your court" comes first because the whole point of
 * this app is that an invoice is somebody's turn, and the person opening the
 * screen needs to know whether it is theirs before anything else.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { api, formatApiError } from "@/lib/api";
import { ErrorBanner } from "@/components/ErrorBanner";
import { useCurrentUser } from "@/lib/useCurrentUser";
import {
  FLAG_LABELS,
  INVOICE_STATUS_LABELS,
  userLabel,
  confidenceLabel,
  confidencePillClass,
  fmtDate,
  fmtMoney,
  hasRole,
  statusPillClass,
  type FlagCode,
  type AppUser,
  type BulkAssignResult,
  type Invoice,
  type InvoiceDashboard,
  type InvoiceStatus,
  type PollResult,
  type Project,
  type Vendor,
} from "@/lib/types";

type StatusFilter = "all" | "open" | InvoiceStatus;

const STATUS_CHIPS: [StatusFilter, string][] = [
  ["open", "Open"],
  ["all", "All"],
  ["suggested", "Suggested"],
  ["assigned", "In review"],
  ["pending_approval", "Pending approval"],
  ["approved", "Approved"],
  ["flagged", "Flagged"],
];

export default function InvoicesPage() {
  const { user } = useCurrentUser();
  const [data, setData] = useState<InvoiceDashboard | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [vendors, setVendors] = useState<Vendor[]>([]);
  const [users, setUsers] = useState<AppUser[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkReviewer, setBulkReviewer] = useState("");
  const [bulkApprover, setBulkApprover] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [polling, setPolling] = useState(false);

  const [status, setStatus] = useState<StatusFilter>("open");
  const [projectId, setProjectId] = useState("");
  const [vendorId, setVendorId] = useState("");
  const [search, setSearch] = useState("");

  const canRunIntake = hasRole(user, "admin", "accountant");

  const load = useCallback(async () => {
    const params = new URLSearchParams();
    if (status !== "all") params.set("status", status);
    if (projectId) params.set("project_id", projectId);
    if (vendorId) params.set("vendor_id", vendorId);
    const qs = params.toString();
    try {
      setData(await api.get<InvoiceDashboard>(`/invoices${qs ? `?${qs}` : ""}`));
    } catch (e) {
      setError(formatApiError(e));
    }
  }, [status, projectId, vendorId]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    Promise.all([
      api.get<Project[]>("/projects"),
      api.get<Vendor[]>("/admin/vendors?active_only=true"),
      api.get<AppUser[]>("/admin/users"),
    ])
      .then(([p, v, u]) => {
        setProjects(p);
        setVendors(v);
        setUsers(u);
      })
      .catch((e) => setError(formatApiError(e)));
  }, []);

  const filtered = useMemo(() => {
    const rows = data?.invoices ?? [];
    const q = search.trim().toLowerCase();
    if (!q) return rows;
    return rows.filter((i) =>
      `${i.invoice_no ?? ""} ${i.vendor_name ?? ""} ${i.project_name ?? ""} ${
        i.source_filename ?? ""
      }`
        .toLowerCase()
        .includes(q)
    );
  }, [data, search]);

  async function runIntake() {
    setPolling(true);
    setError(null);
    setNotice(null);
    try {
      const result = await api.post<PollResult>("/jobs/poll-drive");
      const parts = [
        `${result.scanned} file${result.scanned === 1 ? "" : "s"} scanned`,
        `${result.ingested} new`,
        `${result.suggested} suggested`,
        `${result.flagged} flagged`,
      ];
      if (result.skipped_existing) {
        parts.push(`${result.skipped_existing} already ingested`);
      }
      setNotice(parts.join(" · "));
      if (result.errors.length > 0) setError(result.errors.join(" "));
      await load();
    } catch (e) {
      setError(formatApiError(e));
    } finally {
      setPolling(false);
    }
  }

  /**
   * §7.3: "She can also bulk-assign."
   *
   * Only invoices that are actually assignable can be selected — picking a
   * row that is already in review and then being told so one at a time is
   * worse than not being able to pick it.
   */
  const assignable = useMemo(
    () =>
      (data?.invoices ?? []).filter((i) =>
        ["ingested", "suggested", "flagged"].includes(i.status)
      ),
    [data]
  );

  async function runBulkAssign() {
    setPolling(true);
    setError(null);
    setNotice(null);
    try {
      const result = await api.post<BulkAssignResult>("/invoices/bulk-assign", {
        invoice_ids: [...selected],
        reviewer_id: bulkReviewer || null,
        approver_id: bulkApprover || null,
      });
      const parts = [`${result.assigned} assigned`];
      if (result.failed) parts.push(`${result.failed} could not be`);
      setNotice(parts.join(" · "));
      if (result.failed) {
        // Per-invoice reasons, not a bare count: one row that moved state
        // since the page loaded should be nameable.
        setError(
          result.results
            .filter((r) => !r.ok)
            .map((r) => r.detail)
            .filter(Boolean)
            .join(" ")
        );
      }
      setSelected(new Set());
      await load();
    } catch (e) {
      setError(formatApiError(e));
    } finally {
      setPolling(false);
    }
  }

  const stats = data?.stats;

  return (
    <>
      <div className="page-header">
        <div className="page-title-block">
          <div className="page-eyebrow">Ferrocrete Builders, Inc.</div>
          <h1 className="page-title">Invoices</h1>
          <div className="page-meta">
            Nothing reaches BuilderTrend without a human reviewer and a human
            approver.
          </div>
        </div>
        {canRunIntake && (
          <div className="page-actions">
            <button className="btn" disabled={polling} onClick={runIntake}>
              {polling ? "Scanning Drive…" : "Run intake now"}
            </button>
          </div>
        )}
      </div>

      <div className="page-content">
        {error && <ErrorBanner message={error} onDismiss={() => setError(null)} />}
        {notice && (
          <div className="callout callout-ok">
            <div className="callout-title">Intake run</div>
            {notice}
          </div>
        )}

        {/* ─── Your court ─────────────────────────────────────────── */}
        {data?.your_court && data.your_court.length > 0 && (
          <div
            className="glass section-card"
            style={{
              marginBottom: 22,
              borderColor: "var(--accent-border)",
            }}
          >
            <div className="section-header">
              <div className="section-title">
                {data.your_court_label ?? "Your court"}
              </div>
              <span className="pill pill-amber">
                {data.your_court.length} waiting
              </span>
            </div>
            <InvoiceTable rows={data.your_court} compact />
          </div>
        )}

        {/* ─── Stat cards ─────────────────────────────────────────── */}
        <div className="dash-stat-grid">
          <StatCard
            label="Unassigned"
            value={stats?.unassigned}
            detail="Read and coded, nobody on it yet"
          />
          <StatCard
            label="In review"
            value={stats?.in_review}
            detail="With a project engineer"
          />
          <StatCard
            label="Pending approval"
            value={stats?.pending_approval}
            detail="Reviewed, awaiting an approver"
          />
          <StatCard
            label="Approved, not uploaded"
            value={stats?.approved_not_uploaded}
            detail="Ready for a Chrome session"
            highlight={(stats?.approved_not_uploaded ?? 0) > 0}
          />
          <StatCard
            label="Flagged"
            value={stats?.flagged}
            detail="Needs triage"
            highlight={(stats?.flagged ?? 0) > 0}
            href="/flagged"
          />
        </div>

        {/* ─── Filters ────────────────────────────────────────────── */}
        <div className="glass dash-filter-row">
          <div className="dash-filter-eyebrow">Status</div>
          <div className="dash-chip-group">
            {STATUS_CHIPS.map(([value, label]) => (
              <button
                key={value}
                className={`dash-chip ${status === value ? "dash-chip-active" : ""}`}
                onClick={() => setStatus(value)}
              >
                {label}
              </button>
            ))}
          </div>

          <div className="dash-filter-divider" />

          <select
            className="dash-dropdown"
            value={projectId}
            onChange={(e) => setProjectId(e.target.value)}
          >
            <option value="">All projects</option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.project_no} — {p.name}
              </option>
            ))}
          </select>

          <select
            className="dash-dropdown"
            value={vendorId}
            onChange={(e) => setVendorId(e.target.value)}
          >
            <option value="">All vendors</option>
            {vendors.map((v) => (
              <option key={v.id} value={v.id}>
                {v.invoice_name}
              </option>
            ))}
          </select>

          <div className="dash-filter-spacer" />

          <input
            className="input"
            style={{ maxWidth: 220 }}
            placeholder="Search invoice no., vendor, file"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>

        {/* ─── Bulk assign (§7.3) ─────────────────────────────────── */}
        {canRunIntake && assignable.length > 0 && (
          <div
            className="glass dash-filter-row"
            style={{
              borderColor: selected.size
                ? "var(--accent-border)"
                : "var(--border)",
            }}
          >
            <div className="dash-filter-eyebrow">Bulk assign</div>

            <button
              className="dash-chip"
              onClick={() =>
                setSelected(
                  selected.size === assignable.length
                    ? new Set()
                    : new Set(assignable.map((i) => i.id))
                )
              }
            >
              {selected.size === assignable.length
                ? "Clear selection"
                : `Select all ${assignable.length} unassigned`}
            </button>

            <span style={{ fontSize: 12.5, color: "var(--text-faint)" }}>
              {selected.size
                ? `${selected.size} selected`
                : "Tick rows below to assign several at once"}
            </span>

            <div className="dash-filter-divider" />

            <select
              className="dash-dropdown"
              value={bulkReviewer}
              onChange={(e) => setBulkReviewer(e.target.value)}
            >
              <option value="">Reviewer: keep each project&rsquo;s PE</option>
              {users
                .filter((u) => u.roles.some((r) => r === "pe" || r === "accountant"))
                .map((u) => (
                  <option key={u.id} value={u.id}>
                    Reviewer: {userLabel(u)}
                  </option>
                ))}
            </select>

            <select
              className="dash-dropdown"
              value={bulkApprover}
              onChange={(e) => setBulkApprover(e.target.value)}
            >
              <option value="">Approver: keep the project default</option>
              {users
                .filter((u) => u.roles.includes("approver"))
                .map((u) => (
                  <option key={u.id} value={u.id}>
                    Approver: {userLabel(u)}
                  </option>
                ))}
            </select>

            <div className="dash-filter-spacer" />

            <button
              className="btn btn-accent"
              disabled={polling || selected.size === 0}
              onClick={runBulkAssign}
            >
              {polling
                ? "Assigning…"
                : `Assign ${selected.size || ""}`.trim()}
            </button>
          </div>
        )}

        {/* ─── Table ──────────────────────────────────────────────── */}
        <div className="glass dash-table">
          {data === null ? (
            <div className="dash-table-empty">Loading…</div>
          ) : filtered.length === 0 ? (
            <EmptyState
              hasAny={(data.invoices ?? []).length > 0}
              canRunIntake={canRunIntake}
            />
          ) : (
            <InvoiceTable
              rows={filtered}
              selectable={canRunIntake}
              selected={selected}
              onToggle={(id) => {
                const next = new Set(selected);
                if (next.has(id)) next.delete(id);
                else next.add(id);
                setSelected(next);
              }}
            />
          )}
        </div>
      </div>
    </>
  );
}

function StatCard({
  label,
  value,
  detail,
  highlight = false,
  href,
}: {
  label: string;
  value: number | undefined;
  detail: string;
  highlight?: boolean;
  href?: string;
}) {
  const body = (
    <>
      <div className="dash-stat-eyebrow">{label}</div>
      <div className="dash-stat-value">{value ?? "—"}</div>
      <div className="dash-stat-subdetail">{detail}</div>
    </>
  );
  const className = `glass dash-stat-card ${
    highlight ? "dash-stat-card-highlight" : ""
  }`;
  return href ? (
    <Link href={href} className={className} style={{ textDecoration: "none" }}>
      {body}
    </Link>
  ) : (
    <div className={className}>{body}</div>
  );
}

function EmptyState({
  hasAny,
  canRunIntake,
}: {
  hasAny: boolean;
  canRunIntake: boolean;
}) {
  if (hasAny) {
    return <div className="dash-table-empty">No invoices match these filters.</div>;
  }
  return (
    <div className="empty-state">
      <div className="empty-state-title">No invoices yet</div>
      <div className="empty-state-desc">
        Intake polls the Drive folders every 15 minutes and picks up anything
        new. Nothing has arrived yet, or Drive is not configured — the
        backend&rsquo;s /health endpoint says which.
      </div>
      {canRunIntake && (
        <div className="empty-state-actions">
          <span style={{ fontSize: 13, color: "var(--text-faint)" }}>
            Use &ldquo;Run intake now&rdquo; above to scan immediately.
          </span>
        </div>
      )}
    </div>
  );
}

function InvoiceTable({
  rows,
  compact = false,
  selectable = false,
  selected,
  onToggle,
}: {
  rows: Invoice[];
  compact?: boolean;
  selectable?: boolean;
  selected?: Set<string>;
  onToggle?: (id: string) => void;
}) {
  // Only an unassigned invoice can be bulk-assigned. Offering a checkbox on a
  // row that would be rejected is a worse experience than no checkbox.
  const canSelect = (i: Invoice) =>
    ["ingested", "suggested", "flagged"].includes(i.status);

  return (
    <div className="table-scroll">
      <table className="data-table">
        <thead>
          <tr>
            {selectable && <th style={{ width: 28 }} />}
            <th>Vendor</th>
            <th>Project</th>
            <th>Invoice no.</th>
            <th>Date</th>
            <th className="num">Amount</th>
            <th>Suggested code</th>
            {!compact && <th>Reviewer</th>}
            {!compact && <th>Approver</th>}
            <th>Status</th>
            <th className="num">Age</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {rows.map((i) => (
            <tr key={i.id}>
              {selectable && (
                <td>
                  {canSelect(i) ? (
                    <input
                      type="checkbox"
                      aria-label={`Select invoice ${i.invoice_no ?? i.id}`}
                      checked={selected?.has(i.id) ?? false}
                      onChange={() => onToggle?.(i.id)}
                    />
                  ) : null}
                </td>
              )}
              <td>
                {i.vendor_name || (
                  <span style={{ color: "var(--ferrocrete-red)" }}>Unknown</span>
                )}
                {i.vendor_bt_name && i.vendor_bt_name !== i.vendor_name && (
                  <span className="cell-sub">BT: {i.vendor_bt_name}</span>
                )}
              </td>
              <td>
                {i.project_name || (
                  <span style={{ color: "var(--ferrocrete-red)" }}>Unmatched</span>
                )}
                {i.project_no && <span className="cell-sub">{i.project_no}</span>}
              </td>
              <td className="mono">
                {i.invoice_no || "—"}
                {i.bill_no && <span className="cell-sub">Bill # {i.bill_no}</span>}
              </td>
              <td>{fmtDate(i.invoice_date)}</td>
              <td className="num">
                {fmtMoney(i.amount)}
                {!i.costs_balanced && i.amount !== null && (
                  <span
                    className="cell-sub"
                    style={{ color: "var(--status-amber)" }}
                  >
                    split off by {fmtMoney(i.costs_difference)}
                  </span>
                )}
              </td>
              <td>
                {i.suggested_code ? (
                  <>
                    <span className="mono" style={{ fontSize: 12.5 }}>
                      {i.suggested_code}
                    </span>
                    <span className="cell-sub">
                      <span
                        className={`pill ${confidencePillClass(
                          i.suggested_confidence
                        )}`}
                      >
                        {confidenceLabel(i.suggested_confidence)}
                      </span>
                    </span>
                  </>
                ) : (
                  <span style={{ color: "var(--text-faint)" }}>None</span>
                )}
              </td>
              {!compact && <td>{i.reviewer_name || "—"}</td>}
              {!compact && <td>{i.approver_name || "—"}</td>}
              <td>
                <span className={`pill ${statusPillClass(i.status)}`}>
                  {INVOICE_STATUS_LABELS[i.status]}
                </span>
                {i.status === "flagged" && i.flag_code && (
                  <span className="cell-sub">
                    {FLAG_LABELS[i.flag_code as FlagCode]}
                  </span>
                )}
              </td>
              <td className="num">
                {i.age_days === null ? "—" : `${i.age_days}d`}
              </td>
              <td>
                <Link href={`/invoices/${i.id}`} className="dash-open-link">
                  Open
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
