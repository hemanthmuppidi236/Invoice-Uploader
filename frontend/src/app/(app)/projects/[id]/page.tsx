"use client";

/**
 * Project detail — `/projects/[id]` (prompt §10)
 *
 * Three jobs: show whether the project is onboarded and what is missing if
 * not, let the fields be edited, and let a new mix design revision be
 * uploaded. Prompt §7.0 versions revisions rather than replacing them, so
 * superseded rows stay on screen, dimmed and read-only.
 */

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { api, formatApiError } from "@/lib/api";
import { ErrorBanner } from "@/components/ErrorBanner";
import {
  MixDesignEditor,
  emptyMixDesign,
  type MixDesignState,
} from "@/components/MixDesignEditor";
import { useCurrentUser } from "@/lib/useCurrentUser";
import {
  PROJECT_SCOPE_LABELS,
  hasRole,
  userLabel,
  type AppUser,
  type CostCode,
  type MixDesignRow,
  type ProjectDetail,
  type ProjectScope,
  type SignedUrl,
} from "@/lib/types";

export default function ProjectDetailPage() {
  const params = useParams<{ id: string }>();
  const projectId = params?.id ?? "";
  const { user } = useCurrentUser();

  const canEdit = hasRole(user, "admin", "accountant");
  const canMapCodes = hasRole(user, "admin", "accountant", "pe");

  const [project, setProject] = useState<ProjectDetail | null>(null);
  const [users, setUsers] = useState<AppUser[]>([]);
  const [costCodes, setCostCodes] = useState<CostCode[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [showRevision, setShowRevision] = useState(false);
  const [newRevision, setNewRevision] = useState<MixDesignState>(emptyMixDesign);

  async function reload() {
    try {
      setProject(await api.get<ProjectDetail>(`/projects/${projectId}`));
    } catch (e) {
      setError(formatApiError(e));
    }
  }

  useEffect(() => {
    if (!projectId) return;
    Promise.all([
      api.get<ProjectDetail>(`/projects/${projectId}`),
      api.get<AppUser[]>("/admin/users"),
      api.get<CostCode[]>("/admin/cost-codes?active_only=true"),
    ])
      .then(([p, u, c]) => {
        setProject(p);
        setUsers(u);
        setCostCodes(c);
      })
      .catch((e) => setError(formatApiError(e)));
  }, [projectId]);

  const codeLabels = useMemo(() => {
    const m = new Map<string, string>();
    costCodes.forEach((c) => m.set(c.id, c.code));
    return m;
  }, [costCodes]);

  const liveRows = useMemo(
    () => (project?.mix_designs ?? []).filter((r) => !r.superseded_at),
    [project]
  );
  const supersededRows = useMemo(
    () => (project?.mix_designs ?? []).filter((r) => r.superseded_at),
    [project]
  );

  async function patchProject(changes: Record<string, unknown>) {
    setBusy(true);
    setError(null);
    try {
      setProject(await api.patch<ProjectDetail>(`/projects/${projectId}`, changes));
      setNotice("Saved.");
    } catch (e) {
      setError(formatApiError(e));
    } finally {
      setBusy(false);
    }
  }

  async function mapRow(rowId: string, costCodeId: string | null) {
    setBusy(true);
    setError(null);
    try {
      await api.patch<MixDesignRow>(
        `/projects/${projectId}/mix-designs/${rowId}`,
        { cost_code_id: costCodeId }
      );
      await reload();
    } catch (e) {
      setError(formatApiError(e));
    } finally {
      setBusy(false);
    }
  }

  async function completeOnboarding() {
    setBusy(true);
    setError(null);
    try {
      setProject(
        await api.post<ProjectDetail>(
          `/projects/${projectId}/complete-onboarding`
        )
      );
      setNotice("Onboarded. Invoices for this project will now route.");
    } catch (e) {
      setError(formatApiError(e));
    } finally {
      setBusy(false);
    }
  }

  async function saveRevision() {
    setBusy(true);
    setError(null);
    try {
      setProject(
        await api.post<ProjectDetail>(`/projects/${projectId}/mix-designs`, {
          revision: newRevision.revision || "CMD-02",
          storage_path: newRevision.storagePath,
          rows: newRevision.rows.map((r) => ({
            mix_no: r.mix_no.trim(),
            psi: r.psi,
            element_use: r.element_use,
            pump_line: r.pump_line,
            cost_code_id: r.cost_code_id,
          })),
        })
      );
      setNewRevision(emptyMixDesign);
      setShowRevision(false);
      setNotice(
        "Revision saved. The previous rows are superseded, not deleted — " +
          "invoices already scored against them keep their basis."
      );
    } catch (e) {
      setError(formatApiError(e));
    } finally {
      setBusy(false);
    }
  }

  async function openSubmittal() {
    try {
      const { url } = await api.get<SignedUrl>(
        `/projects/${projectId}/mix-design-url`
      );
      window.open(url, "_blank", "noopener");
    } catch (e) {
      setError(formatApiError(e));
    }
  }

  if (!project) {
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

  const ob = project.onboarding;
  const reviewers = users.filter((u) =>
    u.roles.some((r) => r === "pe" || r === "accountant")
  );
  const approvers = users.filter((u) => u.roles.includes("approver"));

  return (
    <>
      <div className="page-header">
        <div className="page-title-block">
          <div className="page-eyebrow">
            {project.project_no}
            {project.bt_job_id ? ` · BT job ${project.bt_job_id}` : ""}
          </div>
          <h1 className="page-title">{project.name}</h1>
          <div className="page-meta">
            {project.address || "No address on file"}
            {" · "}
            <strong>{PROJECT_SCOPE_LABELS[project.status]}</strong>
            {" · "}
            {project.invoice_count} invoice
            {project.invoice_count === 1 ? "" : "s"}
          </div>
        </div>
        <div className="page-actions">
          {project.mix_design_pdf_path && (
            <button className="btn" onClick={openSubmittal}>
              Open submittal
            </button>
          )}
          <Link href="/projects" className="btn btn-ghost">
            All projects
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

        {/* ─── Onboarding status ─────────────────────────────────── */}
        <div className="glass section-card" style={{ marginBottom: 20 }}>
          <div className="section-header">
            <div className="section-title">Onboarding</div>
            {project.onboarded_at ? (
              <span className="pill pill-green">Onboarded</span>
            ) : (
              <span className="pill pill-red">Not onboarded</span>
            )}
          </div>

          {project.onboarded_at ? (
            <div className="callout callout-ok">
              <div className="callout-title">Routing</div>
              Invoices matching this project are assigned to{" "}
              {project.pe_name || "the project engineer"} for review.
              {ob && ob.mix_design_rows > ob.mix_design_rows_mapped && (
                <>
                  {" "}
                  {ob.mix_design_rows - ob.mix_design_rows_mapped} of{" "}
                  {ob.mix_design_rows} mixes have no cost code, so those mix
                  numbers will not produce a concrete suggestion.
                </>
              )}
            </div>
          ) : (
            <>
              <div className="callout callout-block">
                <div className="callout-title">
                  {ob && ob.blockers.length > 0
                    ? "Blocked"
                    : "Ready to onboard"}
                </div>
                {ob && ob.blockers.length > 0 ? (
                  <ul>
                    {ob.blockers.map((b, i) => (
                      <li key={i}>{b}</li>
                    ))}
                  </ul>
                ) : (
                  <>
                    Everything required is in place. Until you confirm
                    onboarding, invoices for this project are flagged rather
                    than routed.
                  </>
                )}
              </div>
              {canEdit && ob && ob.blockers.length === 0 && (
                <button
                  className="btn btn-accent"
                  disabled={busy}
                  onClick={completeOnboarding}
                >
                  Complete onboarding
                </button>
              )}
            </>
          )}
        </div>

        {/* ─── Fields ────────────────────────────────────────────── */}
        <div className="glass section-card" style={{ marginBottom: 20 }}>
          <div className="section-header">
            <div className="section-title">Project details</div>
            {!canEdit && <span className="pill pill-muted">Read only</span>}
          </div>

          <div className="form-grid-2">
            <ReadOrEdit
              label="Project number"
              value={project.project_no}
              canEdit={canEdit}
              busy={busy}
              onSave={(v) => patchProject({ project_no: v })}
            />
            <ReadOrEdit
              label="BuilderTrend job name"
              value={project.name}
              canEdit={canEdit}
              busy={busy}
              onSave={(v) => patchProject({ name: v })}
            />
            <ReadOrEdit
              label="BuilderTrend jobId"
              value={project.bt_job_id ?? ""}
              canEdit={canEdit}
              busy={busy}
              onSave={(v) => patchProject({ bt_job_id: v || null })}
            />
            <ReadOrEdit
              label="Site address"
              value={project.address ?? ""}
              canEdit={canEdit}
              busy={busy}
              onSave={(v) => patchProject({ address: v || null })}
            />
            <ReadOrEdit
              label="Drive folder name"
              value={project.drive_folder_name ?? ""}
              canEdit={canEdit}
              busy={busy}
              onSave={(v) => patchProject({ drive_folder_name: v || null })}
              help="Filing looks this folder up by name and never creates one."
            />
            <div className="form-row">
              <label className="form-label">Scope</label>
              {canEdit ? (
                <select
                  className="input"
                  value={project.status}
                  disabled={busy}
                  onChange={(e) =>
                    patchProject({ status: e.target.value as ProjectScope })
                  }
                >
                  {(["old", "active"] as ProjectScope[]).map((s) => (
                    <option key={s} value={s}>
                      {PROJECT_SCOPE_LABELS[s]}
                    </option>
                  ))}
                </select>
              ) : (
                <div style={{ fontSize: 14 }}>
                  {PROJECT_SCOPE_LABELS[project.status]}
                </div>
              )}
            </div>

            <div className="form-row">
              <label className="form-label">Project engineer</label>
              {canEdit ? (
                <select
                  className="input"
                  value={project.pe_user_id ?? ""}
                  disabled={busy}
                  onChange={(e) =>
                    patchProject({ pe_user_id: e.target.value || null })
                  }
                >
                  <option value="">Not assigned</option>
                  {reviewers.map((u) => (
                    <option key={u.id} value={u.id}>
                      {userLabel(u)}
                    </option>
                  ))}
                </select>
              ) : (
                <div style={{ fontSize: 14 }}>{project.pe_name || "—"}</div>
              )}
            </div>

            <div className="form-row">
              <label className="form-label">Default approver</label>
              {canEdit ? (
                <select
                  className="input"
                  value={project.default_approver_id ?? ""}
                  disabled={busy}
                  onChange={(e) =>
                    patchProject({ default_approver_id: e.target.value || null })
                  }
                >
                  <option value="">Set per invoice</option>
                  {approvers.map((u) => (
                    <option key={u.id} value={u.id}>
                      {userLabel(u)}
                    </option>
                  ))}
                </select>
              ) : (
                <div style={{ fontSize: 14 }}>
                  {project.default_approver_name || "—"}
                </div>
              )}
            </div>
          </div>

          {canEdit && (
            <div className="form-checkbox" style={{ marginTop: 6 }}>
              <input
                id="concrete-flag"
                type="checkbox"
                checked={project.has_concrete_supplier}
                disabled={busy}
                onChange={(e) =>
                  patchProject({ has_concrete_supplier: e.target.checked })
                }
              />
              <label htmlFor="concrete-flag" style={{ fontSize: 13.5 }}>
                This job has a concrete supplier
                <span
                  style={{
                    display: "block",
                    fontSize: 12.5,
                    color: "var(--text-faint)",
                    marginTop: 3,
                  }}
                >
                  Turning this on for an onboarded job with no mix design
                  leaves it onboarded but unable to suggest concrete codes.
                </span>
              </label>
            </div>
          )}
        </div>

        {/* ─── Mix designs ───────────────────────────────────────── */}
        <div className="glass section-card" style={{ marginBottom: 20 }}>
          <div className="section-header">
            <div className="section-title">
              Mix design
              {liveRows[0]?.revision ? ` · ${liveRows[0].revision}` : ""}
            </div>
            {canEdit && (
              <button
                className="btn"
                onClick={() => setShowRevision((v) => !v)}
              >
                {showRevision ? "Cancel revision" : "Upload a new revision"}
              </button>
            )}
          </div>

          {liveRows.length === 0 && !showRevision && (
            <div className="callout callout-info">
              <div className="callout-title">No mix design on file</div>
              {project.has_concrete_supplier
                ? "This job needs one before it can be onboarded. Upload the supplier's submittal."
                : "This job is marked as having no concrete supplier, so none is required."}
            </div>
          )}

          {liveRows.length > 0 && (
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Mix no.</th>
                    <th className="num">PSI</th>
                    <th>Element uses</th>
                    <th>Pump line</th>
                    <th>Cost code</th>
                  </tr>
                </thead>
                <tbody>
                  {liveRows.map((r) => (
                    <tr key={r.id}>
                      <td className="mono">{r.mix_no}</td>
                      <td className="num">{r.psi ?? "—"}</td>
                      <td>
                        {r.element_use.length > 0 ? (
                          <div className="chip-row">
                            {r.element_use.map((e) => (
                              <span key={e} className="chip">
                                {e}
                              </span>
                            ))}
                          </div>
                        ) : (
                          "—"
                        )}
                      </td>
                      <td className="mono">{r.pump_line ?? "—"}</td>
                      <td>
                        {canMapCodes ? (
                          <select
                            className="cell-select"
                            value={r.cost_code_id ?? ""}
                            disabled={busy}
                            onChange={(e) =>
                              mapRow(r.id, e.target.value || null)
                            }
                          >
                            <option value="">Not mapped</option>
                            {costCodes.map((c) => (
                              <option key={c.id} value={c.id}>
                                {c.code}
                              </option>
                            ))}
                          </select>
                        ) : (
                          r.cost_code || "Not mapped"
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {showRevision && (
            <div style={{ marginTop: 22 }}>
              <div className="callout callout-info">
                <div className="callout-title">New revision</div>
                Saving this supersedes the rows above without deleting them.
                Invoices already scored against the old rows keep their basis.
              </div>
              <MixDesignEditor
                value={newRevision}
                onChange={setNewRevision}
                costCodes={costCodes}
                projectId={project.id}
                projectNo={project.project_no}
                projectName={project.name}
              />
              <div className="form-actions">
                <button
                  className="btn btn-accent"
                  disabled={busy || newRevision.rows.length === 0}
                  onClick={saveRevision}
                >
                  {busy ? "Saving…" : "Save revision"}
                </button>
                <button
                  className="btn btn-ghost"
                  onClick={() => {
                    setShowRevision(false);
                    setNewRevision(emptyMixDesign);
                  }}
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          {supersededRows.length > 0 && (
            <div style={{ marginTop: 26 }}>
              <div className="sov-section-label">
                Superseded revisions ({supersededRows.length} rows)
              </div>
              <div className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Revision</th>
                      <th>Mix no.</th>
                      <th className="num">PSI</th>
                      <th>Element uses</th>
                      <th>Cost code</th>
                    </tr>
                  </thead>
                  <tbody>
                    {supersededRows.map((r) => (
                      <tr key={r.id} className="row-superseded">
                        <td className="mono">{r.revision}</td>
                        <td className="mono">{r.mix_no}</td>
                        <td className="num">{r.psi ?? "—"}</td>
                        <td>{r.element_use.join(", ") || "—"}</td>
                        <td>{r.cost_code || codeLabels.get(r.cost_code_id ?? "") || "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>

        {/* ─── Notes ─────────────────────────────────────────────── */}
        <div className="glass section-card">
          <div className="section-header">
            <div className="section-title">Notes</div>
          </div>
          {canEdit ? (
            <ReadOrEdit
              label=""
              value={project.notes ?? ""}
              canEdit
              busy={busy}
              multiline
              onSave={(v) => patchProject({ notes: v || null })}
              help="What a reviewer or the upload session should know about this job."
            />
          ) : (
            <div style={{ fontSize: 14, color: "var(--text-body)" }}>
              {project.notes || "None."}
            </div>
          )}
        </div>
      </div>
    </>
  );
}

/**
 * A field that shows its value and saves on blur when changed. Deliberately
 * not auto-saving on a timer: prompt §10 reserves the 900 ms auto-save for
 * the invoice detail form, where the reviewer is typing amounts, not for
 * reference fields where an accidental keystroke should be recoverable.
 */
function ReadOrEdit({
  label,
  value,
  canEdit,
  busy,
  onSave,
  help,
  multiline = false,
}: {
  label: string;
  value: string;
  canEdit: boolean;
  busy: boolean;
  onSave: (v: string) => void;
  help?: string;
  multiline?: boolean;
}) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);

  const id = label ? label.toLowerCase().replace(/[^a-z0-9]+/g, "-") : "notes";

  if (!canEdit) {
    return (
      <div className="form-row">
        {label && <label className="form-label">{label}</label>}
        <div style={{ fontSize: 14 }}>{value || "—"}</div>
      </div>
    );
  }

  const commit = () => {
    if (draft !== value) onSave(draft);
  };

  return (
    <div className="form-row">
      {label && (
        <label className="form-label" htmlFor={id}>
          {label}
        </label>
      )}
      {multiline ? (
        <textarea
          id={id}
          className="input"
          rows={3}
          value={draft}
          disabled={busy}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
        />
      ) : (
        <input
          id={id}
          className="input"
          value={draft}
          disabled={busy}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
        />
      )}
      {help && <div className="form-help">{help}</div>}
    </div>
  );
}
