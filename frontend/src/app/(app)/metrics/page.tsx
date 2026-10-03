"use client";

/**
 * AI accuracy — `/metrics` (prompt §12)
 *
 * "Every AI suggestion and every human edit is stored. Add a small /metrics
 * page showing AI acceptance rate by cost code and vendor."
 *
 * The question underneath is whether to keep trusting the suggestion, so the
 * page leads with the reading rather than the numbers. A table of
 * percentages invites everyone to find their own story in it; the callouts
 * say the two or three things that should actually change what somebody
 * does — a cost code whose keywords need work, or a confidence score that
 * turns out to predict nothing.
 *
 * Every rate is computed server-side. Re-deriving "was this accepted" in the
 * browser would eventually disagree with the backend, and the disagreement
 * would be invisible.
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api, formatApiError } from "@/lib/api";
import { ErrorBanner } from "@/components/ErrorBanner";
import { useCurrentUser } from "@/lib/useCurrentUser";
import {
  fmtDateTime,
  hasRole,
  type Metrics,
  type MetricsBucket,
} from "@/lib/types";

const WINDOWS: [number, string][] = [
  [30, "30 days"],
  [90, "90 days"],
  [365, "A year"],
];

export default function MetricsPage() {
  const { user, loading } = useCurrentUser();
  const [data, setData] = useState<Metrics | null>(null);
  const [windowDays, setWindowDays] = useState(90);
  const [error, setError] = useState<string | null>(null);

  const canView = hasRole(user, "admin", "accountant");

  const load = useCallback(async () => {
    try {
      setData(
        await api.get<Metrics>(`/metrics?window_days=${windowDays}`)
      );
      setError(null);
    } catch (e) {
      setError(formatApiError(e));
    }
  }, [windowDays]);

  useEffect(() => {
    if (canView) load();
  }, [canView, load]);

  if (!loading && !canView) {
    return (
      <>
        <Header />
        <div className="page-content">
          <div className="glass section-card">
            <div className="empty-state">
              <div className="empty-state-title">Not your screen</div>
              <div className="empty-state-desc">
                This is accounting&apos;s view of how well the AI is coding
                invoices.
              </div>
            </div>
          </div>
        </div>
      </>
    );
  }

  return (
    <>
      <Header
        right={
          <div className="dash-chip-group">
            {WINDOWS.map(([days, label]) => (
              <button
                key={days}
                className={`dash-chip ${
                  windowDays === days ? "dash-chip-active" : ""
                }`}
                onClick={() => setWindowDays(days)}
              >
                {label}
              </button>
            ))}
          </div>
        }
      />

      <div className="page-content">
        {error && <ErrorBanner message={error} onDismiss={() => setError(null)} />}

        {!data && !error && (
          <div className="glass section-card">
            <div style={{ color: "var(--text-muted)" }}>Loading…</div>
          </div>
        )}

        {data && (
          <>
            <div className="glass section-card" style={{ marginBottom: 20 }}>
              <div className="section-header">
                <div className="section-title">Overall</div>
                <span className="chip">
                  as of {fmtDateTime(data.generated_at)}
                </span>
              </div>

              {data.judged === 0 ? (
                <div className="empty-state">
                  <div className="empty-state-title">Nothing to score yet</div>
                  <div className="empty-state-desc">
                    Accuracy is measured against invoices a person has
                    approved, because that is the only point where someone has
                    definitely looked. Approve a few and this fills in.
                  </div>
                </div>
              ) : (
                <div className="dash-stat-grid">
                  <Stat
                    label="Suggestions kept"
                    value={pct(data.rate)}
                    sub={`${data.accepted} of ${data.judged} approved invoices`}
                    highlight
                  />
                  <Stat
                    label="Judged"
                    value={String(data.judged)}
                    sub={`approved in the last ${data.window_days} days`}
                  />
                  <Stat
                    label="Not scored"
                    value={String(data.unsuggested)}
                    sub="no AI suggestion to judge"
                  />
                </div>
              )}

              {data.notes.map((note) => (
                <div className="callout callout-info" key={note}>
                  {note}
                </div>
              ))}
            </div>

            <BucketTable
              title="By confidence"
              buckets={data.by_confidence}
              column="Band"
              blurb="The one that decides whether the confidence number is worth anything. If high is accepted no more often than low, the score is decoration — and the colour of the pill on every invoice is decoration with it."
            />

            <BucketTable
              title="By vendor"
              buckets={data.by_vendor}
              column="Vendor"
              blurb="White Cap is always 3015, so it should be near perfect. Ready-mix is the hard case the whole §8 authority order exists for; a low rate there is expected, not alarming."
            />

            <BucketTable
              title="By cost code"
              buckets={data.by_cost_code}
              column="Suggested code"
              blurb="Bucketed by what the AI proposed, not by what was kept — the question is “when it says 3002, is it right”. A code rejected more often than not usually needs better element keywords, which is a fix you can make under Admin."
            />
          </>
        )}
      </div>
    </>
  );
}

function Header({ right }: { right?: React.ReactNode }) {
  return (
    <div className="page-header">
      <div className="page-title-block">
        <div className="page-eyebrow">Ferrocrete Builders, Inc.</div>
        <h1 className="page-title">AI accuracy</h1>
        <div className="page-meta">
          How often the suggested cost code survived to approval
        </div>
      </div>
      <div className="page-actions">
        {right}
        <Link href="/invoices" className="btn btn-ghost">
          Invoices
        </Link>
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  sub,
  highlight = false,
}: {
  label: string;
  value: string;
  sub: string;
  highlight?: boolean;
}) {
  return (
    <div
      className={`dash-stat-card${highlight ? " dash-stat-card-highlight" : ""}`}
    >
      <div className="dash-stat-eyebrow">{label}</div>
      <div className="dash-stat-value">{value}</div>
      <div className="dash-stat-subdetail">{sub}</div>
    </div>
  );
}

function BucketTable({
  title,
  buckets,
  column,
  blurb,
}: {
  title: string;
  buckets: MetricsBucket[];
  column: string;
  blurb: string;
}) {
  if (buckets.length === 0) return null;

  return (
    <div className="glass section-card" style={{ marginBottom: 20 }}>
      <div className="section-header">
        <div className="section-title">{title}</div>
      </div>
      <div style={{ fontSize: 13, color: "var(--text-muted)", marginBottom: 12 }}>
        {blurb}
      </div>
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th>{column}</th>
              <th style={{ width: 100 }}>Kept</th>
              <th style={{ width: 110 }}>Rate</th>
              <th style={{ width: 220 }} />
            </tr>
          </thead>
          <tbody>
            {buckets.map((b) => (
              <tr key={b.key}>
                <td>
                  {b.label}
                  {b.total < 5 && (
                    <span className="cell-sub">
                      too few to read much into
                    </span>
                  )}
                </td>
                <td className="mono">
                  {b.accepted}/{b.total}
                </td>
                <td>
                  <span className={`pill ${ratePill(b)}`}>{pct(b.rate)}</span>
                </td>
                <td>
                  <RateBar bucket={b} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function RateBar({ bucket }: { bucket: MetricsBucket }) {
  if (bucket.rate === null) return null;
  return (
    <div
      style={{
        height: 8,
        borderRadius: 4,
        background: "var(--accent-dim)",
        overflow: "hidden",
      }}
      aria-hidden
    >
      <div
        style={{
          width: `${Math.round(bucket.rate * 100)}%`,
          height: "100%",
          background:
            bucket.rate >= 0.8
              ? "var(--status-green)"
              : bucket.rate >= 0.5
                ? "var(--status-amber)"
                : "var(--status-red)",
        }}
      />
    </div>
  );
}

/** Low volume stays neutral: a red pill on two invoices reads as a verdict. */
function ratePill(b: MetricsBucket): string {
  if (b.rate === null || b.total < 5) return "pill-muted";
  if (b.rate >= 0.8) return "pill-green";
  if (b.rate >= 0.5) return "pill-amber";
  return "pill-red";
}

/** "—" rather than "0%" when there is nothing to judge. */
function pct(rate: number | null): string {
  if (rate === null) return "—";
  return `${Math.round(rate * 100)}%`;
}
