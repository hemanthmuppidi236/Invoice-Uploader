"use client";

/**
 * Projects list — `/projects`
 *
 * The onboarding status badge is the point of this screen. Prompt §7.0 makes
 * onboarding the gate on invoice routing, so "which projects are not ready"
 * is the question this page has to answer at a glance, ahead of anything else.
 */

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { api, formatApiError } from "@/lib/api";
import { ErrorBanner } from "@/components/ErrorBanner";
import { useCurrentUser } from "@/lib/useCurrentUser";
import {
  PROJECT_SCOPE_LABELS,
  type Project,
  type ProjectScope,
  hasRole,
} from "@/lib/types";

type ScopeFilter = "all" | ProjectScope;
type ReadyFilter = "all" | "ready" | "blocked";

export default function ProjectsPage() {
  const { user } = useCurrentUser();
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [scope, setScope] = useState<ScopeFilter>("all");
  const [ready, setReady] = useState<ReadyFilter>("all");
  const [search, setSearch] = useState("");

  const canOnboard = hasRole(user, "admin", "accountant");

  useEffect(() => {
    api
      .get<Project[]>("/projects")
      .then(setProjects)
      .catch((e) => {
        setError(formatApiError(e));
        setProjects([]);
      });
  }, []);

  const filtered = useMemo(() => {
    if (!projects) return [];
    const q = search.trim().toLowerCase();
    return projects.filter((p) => {
      if (scope !== "all" && p.status !== scope) return false;
      if (ready === "ready" && !p.onboarded_at) return false;
      if (ready === "blocked" && p.onboarded_at) return false;
      if (
        q &&
        !`${p.project_no} ${p.name} ${p.address ?? ""} ${p.bt_job_id ?? ""}`
          .toLowerCase()
          .includes(q)
      )
        return false;
      return true;
    });
  }, [projects, scope, ready, search]);

  const counts = useMemo(() => {
    const all = projects ?? [];
    return {
      total: all.length,
      onboarded: all.filter((p) => p.onboarded_at).length,
      blocked: all.filter((p) => !p.onboarded_at).length,
      newScope: all.filter((p) => p.status === "active").length,
    };
  }, [projects]);

  return (
    <>
      <div className="page-header">
        <div className="page-title-block">
          <div className="page-eyebrow">Ferrocrete Builders, Inc.</div>
          <h1 className="page-title">Projects</h1>
          <div className="page-meta">
            A project routes invoices only once it is onboarded: a project
            engineer to review them, and a confirmed mix design to score
            concrete against.
          </div>
        </div>
        {canOnboard && (
          <div className="page-actions">
            <Link href="/projects/new" className="btn btn-accent">
              Onboard a project
            </Link>
          </div>
        )}
      </div>

      <div className="page-content">
        {error && <ErrorBanner message={error} onDismiss={() => setError(null)} />}

        <div className="dash-stat-grid">
          <div className="glass dash-stat-card">
            <div className="dash-stat-eyebrow">Projects</div>
            <div className="dash-stat-value">{counts.total}</div>
          </div>
          <div className="glass dash-stat-card">
            <div className="dash-stat-eyebrow">Onboarded</div>
            <div className="dash-stat-value">{counts.onboarded}</div>
            <div className="dash-stat-subdetail">Routing invoices</div>
          </div>
          <div
            className={`glass dash-stat-card ${
              counts.blocked > 0 ? "dash-stat-card-highlight" : ""
            }`}
          >
            <div className="dash-stat-eyebrow">Not onboarded</div>
            <div className="dash-stat-value">{counts.blocked}</div>
            <div className="dash-stat-subdetail">
              {counts.blocked > 0
                ? "Invoices for these will be flagged"
                : "Nothing pending"}
            </div>
          </div>
          <div className="glass dash-stat-card">
            <div className="dash-stat-eyebrow">New, out of scope</div>
            <div className="dash-stat-value">{counts.newScope}</div>
            <div className="dash-stat-subdetail">Separate workflow</div>
          </div>
        </div>

        <div className="glass dash-filter-row">
          <div className="dash-filter-eyebrow">Scope</div>
          <div className="dash-chip-group">
            {(["all", "old", "active"] as ScopeFilter[]).map((s) => (
              <button
                key={s}
                className={`dash-chip ${scope === s ? "dash-chip-active" : ""}`}
                onClick={() => setScope(s)}
              >
                {s === "all" ? "All" : PROJECT_SCOPE_LABELS[s]}
              </button>
            ))}
          </div>

          <div className="dash-filter-divider" />

          <div className="dash-filter-eyebrow">Status</div>
          <div className="dash-chip-group">
            {(
              [
                ["all", "All"],
                ["ready", "Onboarded"],
                ["blocked", "Not onboarded"],
              ] as [ReadyFilter, string][]
            ).map(([value, label]) => (
              <button
                key={value}
                className={`dash-chip ${ready === value ? "dash-chip-active" : ""}`}
                onClick={() => setReady(value)}
              >
                {label}
              </button>
            ))}
          </div>

          <div className="dash-filter-spacer" />

          <input
            className="input"
            style={{ maxWidth: 240 }}
            placeholder="Search number, name, address"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>

        <div className="glass dash-table">
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Project</th>
                  <th>BT job</th>
                  <th>Project engineer</th>
                  <th>Approver</th>
                  <th>Mix design</th>
                  <th className="num">Invoices</th>
                  <th>Status</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {projects === null && (
                  <tr>
                    <td colSpan={8} className="dash-table-empty">
                      Loading…
                    </td>
                  </tr>
                )}
                {projects !== null && filtered.length === 0 && (
                  <tr>
                    <td colSpan={8} className="dash-table-empty">
                      {projects.length === 0
                        ? "No projects yet. Onboard one to start routing its invoices."
                        : "No projects match these filters."}
                    </td>
                  </tr>
                )}
                {filtered.map((p) => (
                  <ProjectRow key={p.id} project={p} />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </>
  );
}

function ProjectRow({ project: p }: { project: Project }) {
  const ob = p.onboarding;
  const mixLabel = !p.has_concrete_supplier
    ? "Not required"
    : ob && ob.mix_design_rows > 0
      ? `${ob.mix_design_rows_mapped} of ${ob.mix_design_rows} mapped`
      : "None on file";

  return (
    <tr>
      <td>
        <Link href={`/projects/${p.id}`} className="dash-open-link">
          {p.name}
        </Link>
        <span className="cell-sub">
          {p.project_no}
          {p.address ? ` · ${p.address}` : ""}
        </span>
      </td>
      <td className="mono">{p.bt_job_id || "—"}</td>
      <td>
        {p.pe_name || (
          <span style={{ color: "var(--ferrocrete-red)" }}>Not assigned</span>
        )}
      </td>
      <td>{p.default_approver_name || "—"}</td>
      <td>
        {mixLabel}
        {p.has_concrete_supplier &&
          ob &&
          ob.mix_design_rows > ob.mix_design_rows_mapped && (
            <span className="cell-sub">
              Unmapped mixes will not be used to suggest a concrete code.
            </span>
          )}
      </td>
      <td className="num">{p.invoice_count}</td>
      <td>
        {p.status === "active" ? (
          <span className="pill pill-muted">Separate workflow</span>
        ) : p.onboarded_at ? (
          <span className="pill pill-green">Onboarded</span>
        ) : (
          <span className="pill pill-red">Not onboarded</span>
        )}
      </td>
      <td>
        <Link href={`/projects/${p.id}`} className="dash-open-link">
          Open
        </Link>
      </td>
    </tr>
  );
}
