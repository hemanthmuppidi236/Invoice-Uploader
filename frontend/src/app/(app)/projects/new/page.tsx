"use client";

/**
 * Project onboarding — `/projects/new` (prompt §7.0)
 *
 * The §7.0 gate is enforced on the server; this screen mirrors it so the
 * reason Save is unavailable is visible before the click rather than arriving
 * as a 422 afterward. Both checks exist on purpose: the client one is for the
 * person, the server one is the guarantee.
 */

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
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
  type ProjectDetail,
  type ProjectScope,
  type Vendor,
} from "@/lib/types";

interface FormState {
  project_no: string;
  name: string;
  bt_job_id: string;
  address: string;
  pe_user_id: string;
  default_approver_id: string;
  status: ProjectScope;
  drive_folder_name: string;
  has_concrete_supplier: boolean;
  expected_vendor_ids: string[];
  notes: string;
}

const EMPTY_FORM: FormState = {
  project_no: "",
  name: "",
  bt_job_id: "",
  address: "",
  pe_user_id: "",
  default_approver_id: "",
  status: "old",
  drive_folder_name: "",
  has_concrete_supplier: true,
  expected_vendor_ids: [],
  notes: "",
};

export default function NewProjectPage() {
  const router = useRouter();
  const { user, loading: userLoading } = useCurrentUser();
  const canOnboard = hasRole(user, "admin", "accountant");

  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [mix, setMix] = useState<MixDesignState>(emptyMixDesign);
  const [users, setUsers] = useState<AppUser[]>([]);
  const [costCodes, setCostCodes] = useState<CostCode[]>([]);
  const [vendors, setVendors] = useState<Vendor[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    Promise.all([
      api.get<AppUser[]>("/admin/users"),
      api.get<CostCode[]>("/admin/cost-codes?active_only=true"),
      api.get<Vendor[]>("/admin/vendors?active_only=true"),
    ])
      .then(([u, c, v]) => {
        setUsers(u);
        setCostCodes(c);
        setVendors(v);
      })
      .catch((e) => setError(formatApiError(e)));
  }, []);

  const reviewers = useMemo(
    () => users.filter((u) => u.roles.some((r) => r === "pe" || r === "accountant")),
    [users]
  );
  const approvers = useMemo(
    () => users.filter((u) => u.roles.includes("approver")),
    [users]
  );

  /**
   * Mirrors onboarding_blockers() in app/api/projects.py. Kept in sync by
   * hand rather than served from the API, because this has to answer before
   * the project exists and so has nothing to ask about.
   */
  const blockers = useMemo(() => {
    const out: string[] = [];
    if (!form.project_no.trim()) out.push("A project number is required.");
    if (!form.name.trim()) out.push("The BuilderTrend job name is required.");
    if (!form.pe_user_id) out.push("No project engineer assigned.");
    if (form.has_concrete_supplier && mix.rows.length === 0) {
      out.push(
        "No mix design on file. Upload the supplier's submittal, or clear " +
          "the concrete supplier checkbox if this job has no ready-mix scope."
      );
    }
    if (mix.rows.some((r) => !r.mix_no.trim())) {
      out.push("Every mix row needs a mix number.");
    }
    const mixNos = mix.rows.map((r) => r.mix_no.trim()).filter(Boolean);
    const dupes = mixNos.filter((m, i) => mixNos.indexOf(m) !== i);
    if (dupes.length > 0) {
      out.push(`Duplicate mix numbers: ${[...new Set(dupes)].join(", ")}`);
    }
    return out;
  }, [form, mix]);

  async function save() {
    setError(null);
    setSaving(true);
    try {
      const created = await api.post<ProjectDetail>("/projects", {
        project_no: form.project_no.trim(),
        name: form.name.trim(),
        bt_job_id: form.bt_job_id.trim() || null,
        address: form.address.trim() || null,
        pe_user_id: form.pe_user_id || null,
        default_approver_id: form.default_approver_id || null,
        status: form.status,
        drive_folder_name: form.drive_folder_name.trim() || null,
        has_concrete_supplier: form.has_concrete_supplier,
        expected_vendor_ids: form.expected_vendor_ids,
        quirks: {},
        notes: form.notes.trim() || null,
        mix_design_revision: mix.revision || "CMD-01",
        mix_design_storage_path: mix.storagePath,
        mix_design_rows: mix.rows.map((r) => ({
          mix_no: r.mix_no.trim(),
          psi: r.psi,
          element_use: r.element_use,
          pump_line: r.pump_line,
          cost_code_id: r.cost_code_id,
        })),
        complete_onboarding: true,
      });
      router.push(`/projects/${created.id}`);
    } catch (e) {
      setError(formatApiError(e));
      setSaving(false);
    }
  }

  if (userLoading) {
    return (
      <div className="page-content" style={{ paddingTop: 36 }}>
        <div style={{ color: "var(--text-muted)" }}>Loading…</div>
      </div>
    );
  }

  if (!canOnboard) {
    return (
      <>
        <div className="page-header">
          <div className="page-title-block">
            <h1 className="page-title">Onboard a project</h1>
          </div>
        </div>
        <div className="page-content">
          <div className="glass section-card">
            <div className="callout callout-block">
              <div className="callout-title">Not allowed</div>
              Onboarding a project is limited to accounting and admin. Ask
              Linda or Hemanth to add this project.
            </div>
            <Link href="/projects" className="btn">
              Back to projects
            </Link>
          </div>
        </div>
      </>
    );
  }

  return (
    <>
      <div className="page-header">
        <div className="page-title-block">
          <div className="page-eyebrow">Project onboarding</div>
          <h1 className="page-title">Onboard a project</h1>
          <div className="page-meta">
            Nothing is saved until you press Save. The mix design is read by
            Claude and confirmed by you first.
          </div>
        </div>
        <div className="page-actions">
          <Link href="/projects" className="btn btn-ghost">
            Cancel
          </Link>
        </div>
      </div>

      <div className="page-content">
        {error && <ErrorBanner message={error} onDismiss={() => setError(null)} />}

        <div className="glass section-card">
          {/* ─── Step 1 ─────────────────────────────────────────── */}
          <div className="onboard-step">
            <div className="onboard-step-head">
              <span className="onboard-step-num">01</span>
              <span className="onboard-step-title">Project details</span>
              <span className="onboard-step-hint">
                The BuilderTrend job name and jobId have to match BuilderTrend
                exactly. The Chrome session opens bills at
                /app/Bills/Bill/0/&#123;jobId&#125;.
              </span>
            </div>

            <div className="form-grid-2">
              <Field
                label="Project number"
                value={form.project_no}
                onChange={(v) => setForm({ ...form, project_no: v })}
                placeholder="25-20"
                help="Ferrocrete's own number, as used on pay apps."
              />
              <Field
                label="BuilderTrend job name"
                value={form.name}
                onChange={(v) => setForm({ ...form, name: v })}
                placeholder="A Street Flats"
                help="Spelled as BuilderTrend spells it."
              />
              <Field
                label="BuilderTrend jobId"
                value={form.bt_job_id}
                onChange={(v) => setForm({ ...form, bt_job_id: v })}
                placeholder="123456"
                help="From the BuilderTrend job URL."
              />
              <Field
                label="Site address"
                value={form.address}
                onChange={(v) => setForm({ ...form, address: v })}
                placeholder="2935 A Street, San Diego"
                help="How invoices usually identify the job."
              />
              <Field
                label="Drive folder name"
                value={form.drive_folder_name}
                onChange={(v) => setForm({ ...form, drive_folder_name: v })}
                placeholder="A Street Flats"
                help="The folder under BT Invoices/. Filing looks it up by name and never creates one."
              />
              <div className="form-row">
                <label className="form-label" htmlFor="scope">
                  Scope
                </label>
                <select
                  id="scope"
                  className="input"
                  value={form.status}
                  onChange={(e) =>
                    setForm({ ...form, status: e.target.value as ProjectScope })
                  }
                >
                  {(["old", "active"] as ProjectScope[]).map((s) => (
                    <option key={s} value={s}>
                      {PROJECT_SCOPE_LABELS[s]}
                    </option>
                  ))}
                </select>
                <div className="form-help">
                  Only legacy jobs route through this app. Invoices matching a
                  new job are flagged and left in place.
                </div>
              </div>
            </div>

            {form.status === "active" && (
              <div className="callout callout-warn" style={{ marginTop: 14 }}>
                <div className="callout-title">This job is out of scope</div>
                Invoices matching it will be flagged with reason
                &ldquo;new project&rdquo; and never routed for review. Record it
                here only so intake can recognise and flag it.
              </div>
            )}
          </div>

          {/* ─── Step 2 ─────────────────────────────────────────── */}
          <div className="onboard-step">
            <div className="onboard-step-head">
              <span className="onboard-step-num">02</span>
              <span className="onboard-step-title">People</span>
              <span className="onboard-step-hint">
                The project engineer is the default reviewer on every invoice
                for this job. Linda can override it per invoice.
              </span>
            </div>

            <div className="form-grid-2">
              <div className="form-row">
                <label className="form-label" htmlFor="pe">
                  Project engineer (default reviewer)
                </label>
                <select
                  id="pe"
                  className="input"
                  value={form.pe_user_id}
                  onChange={(e) =>
                    setForm({ ...form, pe_user_id: e.target.value })
                  }
                >
                  <option value="">Not assigned</option>
                  {reviewers.map((u) => (
                    <option key={u.id} value={u.id}>
                      {userLabel(u)}
                    </option>
                  ))}
                </select>
                <div className="form-help">
                  Required. Without a reviewer, invoices for this job are
                  flagged rather than routed.
                </div>
              </div>

              <div className="form-row">
                <label className="form-label" htmlFor="approver">
                  Default approver
                </label>
                <select
                  id="approver"
                  className="input"
                  value={form.default_approver_id}
                  onChange={(e) =>
                    setForm({ ...form, default_approver_id: e.target.value })
                  }
                >
                  <option value="">Set per invoice</option>
                  {approvers.map((u) => (
                    <option key={u.id} value={u.id}>
                      {userLabel(u)}
                    </option>
                  ))}
                </select>
                <div className="form-help">
                  Optional. The reviewer can change it when marking reviewed.
                </div>
              </div>
            </div>

            {reviewers.length === 0 && (
              <div className="callout callout-warn">
                <div className="callout-title">No reviewers on the roster</div>
                Nobody holds the project engineer or accounting role yet. Add
                them under Admin &rarr; Users first, then come back.
              </div>
            )}
          </div>

          {/* ─── Step 3 ─────────────────────────────────────────── */}
          <div className="onboard-step">
            <div className="onboard-step-head">
              <span className="onboard-step-num">03</span>
              <span className="onboard-step-title">Mix design</span>
              <span className="onboard-step-hint">
                The yardage sheet maps each mix number to the elements it
                serves. That mapping is how a mix number on a ready-mix
                invoice becomes a cost code.
              </span>
            </div>

            <div className="form-checkbox" style={{ marginBottom: 18 }}>
              <input
                id="concrete"
                type="checkbox"
                checked={form.has_concrete_supplier}
                onChange={(e) =>
                  setForm({ ...form, has_concrete_supplier: e.target.checked })
                }
              />
              <label htmlFor="concrete" style={{ fontSize: 13.5 }}>
                This job has a concrete supplier
                <span
                  style={{
                    display: "block",
                    fontSize: 12.5,
                    color: "var(--text-faint)",
                    marginTop: 3,
                  }}
                >
                  Leave checked for any job with ready-mix scope. A mix design
                  is then required before the job can be onboarded. Clear it
                  only for jobs with no concrete, where invoices route on
                  vendor defaults instead.
                </span>
              </label>
            </div>

            <MixDesignEditor
              value={mix}
              onChange={setMix}
              costCodes={costCodes}
              projectNo={form.project_no}
              projectName={form.name}
            />
          </div>

          {/* ─── Step 4 ─────────────────────────────────────────── */}
          <div className="onboard-step">
            <div className="onboard-step-head">
              <span className="onboard-step-num">04</span>
              <span className="onboard-step-title">
                Expected vendors and notes
              </span>
              <span className="onboard-step-hint">
                Optional. Naming the vendors you expect narrows AI vendor
                matching on this job.
              </span>
            </div>

            <div className="form-row">
              <label className="form-label">Expected vendors</label>
              <div className="chip-row">
                {vendors.length === 0 && (
                  <span style={{ fontSize: 13, color: "var(--text-faint)" }}>
                    No vendors loaded yet.
                  </span>
                )}
                {vendors.map((v) => {
                  const on = form.expected_vendor_ids.includes(v.id);
                  return (
                    <button
                      key={v.id}
                      type="button"
                      className={`dash-chip ${on ? "dash-chip-active" : ""}`}
                      onClick={() =>
                        setForm({
                          ...form,
                          expected_vendor_ids: on
                            ? form.expected_vendor_ids.filter((x) => x !== v.id)
                            : [...form.expected_vendor_ids, v.id],
                        })
                      }
                    >
                      {v.invoice_name}
                    </button>
                  );
                })}
              </div>
              <div className="form-help">
                Leave all unselected to accept any vendor.
              </div>
            </div>

            <div className="form-row">
              <label className="form-label" htmlFor="notes">
                Notes
              </label>
              <textarea
                id="notes"
                className="input"
                rows={3}
                value={form.notes}
                onChange={(e) => setForm({ ...form, notes: e.target.value })}
                placeholder="Anything the reviewer or the upload session should know about this job."
              />
            </div>
          </div>

          {/* ─── Save ───────────────────────────────────────────── */}
          {blockers.length > 0 ? (
            <div className="callout callout-block">
              <div className="callout-title">
                Before this project can be onboarded
              </div>
              <ul>
                {blockers.map((b, i) => (
                  <li key={i}>{b}</li>
                ))}
              </ul>
            </div>
          ) : (
            <div className="callout callout-ok">
              <div className="callout-title">Ready</div>
              Saving onboards {form.name || "this project"} and starts routing
              its invoices to {userLabel(users.find((u) => u.id === form.pe_user_id))}.
            </div>
          )}

          <div className="form-actions">
            <button
              className="btn btn-accent"
              disabled={blockers.length > 0 || saving}
              onClick={save}
            >
              {saving ? "Saving…" : "Save and onboard"}
            </button>
            <Link href="/projects" className="btn btn-ghost">
              Cancel
            </Link>
          </div>
        </div>
      </div>
    </>
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
  help,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  help?: string;
}) {
  const id = label.toLowerCase().replace(/[^a-z0-9]+/g, "-");
  return (
    <div className="form-row">
      <label className="form-label" htmlFor={id}>
        {label}
      </label>
      <input
        id={id}
        className="input"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
      />
      {help && <div className="form-help">{help}</div>}
    </div>
  );
}
