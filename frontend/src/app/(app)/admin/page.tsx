"use client";

/**
 * Reference data — `/admin` (prompt §10)
 *
 * The whole point of this screen is that the §15 kickoff data is data entry,
 * not a code change: cost codes, the vendor name mapping, the roster and its
 * roles, and the end-of-day distribution list are all editable here.
 *
 * Nothing here creates anything in BuilderTrend. Adding a cost code or a
 * vendor records something that already exists there so the app can select
 * it — prompt §12 forbids the app or the agent from creating either.
 */

import { useEffect, useMemo, useState } from "react";
import { api, formatApiError } from "@/lib/api";
import { ErrorBanner } from "@/components/ErrorBanner";
import { useCurrentUser } from "@/lib/useCurrentUser";
import {
  ALL_ROLES,
  ROLE_DESCRIPTIONS,
  ROLE_LABELS,
  hasRole,
  type AppUser,
  type CostCode,
  type DistributionEntry,
  type Role,
  type Vendor,
} from "@/lib/types";

type Tab = "cost-codes" | "vendors" | "users" | "distribution";

const TABS: [Tab, string][] = [
  ["cost-codes", "Cost codes"],
  ["vendors", "Vendors"],
  ["users", "Users and roles"],
  ["distribution", "Distribution list"],
];

export default function AdminPage() {
  const { user, loading } = useCurrentUser();
  const [tab, setTab] = useState<Tab>("cost-codes");
  const [error, setError] = useState<string | null>(null);

  const canWrite = hasRole(user, "admin", "accountant");
  const isAdmin = hasRole(user, "admin");

  if (loading) {
    return (
      <div className="page-content" style={{ paddingTop: 36 }}>
        <div style={{ color: "var(--text-muted)" }}>Loading…</div>
      </div>
    );
  }

  return (
    <>
      <div className="page-header">
        <div className="page-title-block">
          <div className="page-eyebrow">Ferrocrete Builders, Inc.</div>
          <h1 className="page-title">Reference data</h1>
          <div className="page-meta">
            Cost codes, the vendor name mapping, the roster, and the end-of-day
            distribution list. Editing these never changes anything in
            BuilderTrend.
          </div>
        </div>
      </div>

      <div className="page-content">
        {error && <ErrorBanner message={error} onDismiss={() => setError(null)} />}

        <div className="tab-row">
          {TABS.map(([value, label]) => (
            <button
              key={value}
              className={`dash-chip ${tab === value ? "dash-chip-active" : ""}`}
              onClick={() => setTab(value)}
            >
              {label}
            </button>
          ))}
        </div>

        {tab === "cost-codes" && (
          <CostCodesPanel canWrite={canWrite} onError={setError} />
        )}
        {tab === "vendors" && (
          <VendorsPanel canWrite={canWrite} onError={setError} />
        )}
        {tab === "users" && (
          <UsersPanel isAdmin={isAdmin} onError={setError} />
        )}
        {tab === "distribution" && (
          <DistributionPanel canWrite={canWrite} onError={setError} />
        )}
      </div>
    </>
  );
}

// ─── Cost codes ───────────────────────────────────────────────────────

function CostCodesPanel({
  canWrite,
  onError,
}: {
  canWrite: boolean;
  onError: (m: string) => void;
}) {
  const [codes, setCodes] = useState<CostCode[] | null>(null);
  const [search, setSearch] = useState("");
  const [showInactive, setShowInactive] = useState(false);
  const [busy, setBusy] = useState(false);

  async function reload() {
    try {
      setCodes(
        await api.get<CostCode[]>(
          `/admin/cost-codes?active_only=${!showInactive}`
        )
      );
    } catch (e) {
      onError(formatApiError(e));
    }
  }

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showInactive]);

  const filtered = useMemo(() => {
    if (!codes) return [];
    const q = search.trim().toLowerCase();
    if (!q) return codes;
    return codes.filter((c) =>
      `${c.code} ${c.description} ${c.element_keywords.join(" ")}`
        .toLowerCase()
        .includes(q)
    );
  }, [codes, search]);

  const byCategory = useMemo(() => {
    const groups = new Map<string, CostCode[]>();
    filtered.forEach((c) => {
      const key = c.category || "Uncategorised";
      groups.set(key, [...(groups.get(key) ?? []), c]);
    });
    return [...groups.entries()];
  }, [filtered]);

  async function toggleActive(c: CostCode) {
    setBusy(true);
    try {
      await api.patch(`/admin/cost-codes/${c.id}`, { active: !c.active });
      await reload();
    } catch (e) {
      onError(formatApiError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="glass section-card">
      <div className="section-header">
        <div className="section-title">
          Cost codes {codes && `(${codes.length})`}
        </div>
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          <label
            style={{
              fontSize: 12.5,
              color: "var(--text-muted)",
              display: "flex",
              gap: 6,
              alignItems: "center",
            }}
          >
            <input
              type="checkbox"
              checked={showInactive}
              onChange={(e) => setShowInactive(e.target.checked)}
            />
            Show inactive
          </label>
          <input
            className="input"
            style={{ maxWidth: 220 }}
            placeholder="Search code or keyword"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
      </div>

      <div className="callout callout-info">
        <div className="callout-title">How these are used</div>
        The base number (3002) goes in the BuilderTrend Bill&rsquo;s Title
        field; the full sub-code goes in the Costs row. The element keywords
        are what the AI matches a mix design&rsquo;s element uses and an
        invoice line&rsquo;s description text against. Inactive codes exist in
        BuilderTrend but must never receive a vendor bill.
      </div>

      {codes === null && (
        <div style={{ color: "var(--text-muted)" }}>Loading…</div>
      )}

      {byCategory.map(([category, rows]) => (
        <div key={category} style={{ marginBottom: 22 }}>
          <div className="sov-section-label">
            {category} ({rows.length})
          </div>
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Code</th>
                  <th>Base</th>
                  <th>Element keywords</th>
                  <th>Active</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((c) => (
                  <tr key={c.id}>
                    <td className="mono">{c.code}</td>
                    <td className="mono">{c.base_code || "—"}</td>
                    <td>
                      {c.element_keywords.length > 0 ? (
                        <div className="chip-row">
                          {c.element_keywords.map((k) => (
                            <span key={k} className="chip">
                              {k}
                            </span>
                          ))}
                        </div>
                      ) : (
                        <span style={{ color: "var(--text-faint)" }}>
                          None — the AI cannot match this code by element
                        </span>
                      )}
                    </td>
                    <td>
                      {canWrite ? (
                        <button
                          className="btn btn-ghost"
                          style={{ padding: "4px 10px", fontSize: 12 }}
                          disabled={busy}
                          onClick={() => toggleActive(c)}
                        >
                          {c.active ? "Deactivate" : "Activate"}
                        </button>
                      ) : (
                        <span
                          className={`pill ${c.active ? "pill-green" : "pill-muted"}`}
                        >
                          {c.active ? "Active" : "Inactive"}
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
  );
}

// ─── Vendors ──────────────────────────────────────────────────────────

function VendorsPanel({
  canWrite,
  onError,
}: {
  canWrite: boolean;
  onError: (m: string) => void;
}) {
  const [vendors, setVendors] = useState<Vendor[] | null>(null);
  const [codes, setCodes] = useState<CostCode[]>([]);
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState({
    invoice_name: "",
    bt_name: "",
    drive_folder_name: "",
    default_cost_code_id: "",
    is_concrete_supplier: false,
    notes: "",
  });

  async function reload() {
    try {
      const [v, c] = await Promise.all([
        api.get<Vendor[]>("/admin/vendors?active_only=false"),
        api.get<CostCode[]>("/admin/cost-codes?active_only=true"),
      ]);
      setVendors(v);
      setCodes(c);
    } catch (e) {
      onError(formatApiError(e));
    }
  }

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function save() {
    setBusy(true);
    try {
      await api.post("/admin/vendors", {
        invoice_name: draft.invoice_name.trim(),
        bt_name: draft.bt_name.trim(),
        drive_folder_name: draft.drive_folder_name.trim() || null,
        default_cost_code_id: draft.default_cost_code_id || null,
        is_concrete_supplier: draft.is_concrete_supplier,
        aliases: [],
        notes: draft.notes.trim() || null,
        active: true,
      });
      setDraft({
        invoice_name: "",
        bt_name: "",
        drive_folder_name: "",
        default_cost_code_id: "",
        is_concrete_supplier: false,
        notes: "",
      });
      setAdding(false);
      await reload();
    } catch (e) {
      onError(formatApiError(e));
    } finally {
      setBusy(false);
    }
  }

  async function patch(v: Vendor, changes: Record<string, unknown>) {
    setBusy(true);
    try {
      await api.patch(`/admin/vendors/${v.id}`, changes);
      await reload();
    } catch (e) {
      onError(formatApiError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="glass section-card">
      <div className="section-header">
        <div className="section-title">
          Vendors {vendors && `(${vendors.length})`}
        </div>
        {canWrite && (
          <button className="btn" onClick={() => setAdding((v) => !v)}>
            {adding ? "Cancel" : "Add a vendor"}
          </button>
        )}
      </div>

      <div className="callout callout-info">
        <div className="callout-title">The three names</div>
        The letterhead name is what the AI reads off the PDF. The BuilderTrend
        name is what the Chrome session types into &ldquo;Pay to&rdquo; — these
        differ often, which is the single most common cause of a stalled
        upload. The Drive folder name is a third spelling in some cases, and
        filing uses it verbatim because it never creates a folder.
      </div>

      {adding && (
        <div
          style={{
            border: "1px solid var(--border)",
            borderRadius: "var(--radius)",
            padding: 18,
            marginBottom: 20,
          }}
        >
          <div className="form-grid-2">
            <div className="form-row">
              <label className="form-label">Name on the invoice</label>
              <input
                className="input"
                value={draft.invoice_name}
                onChange={(e) =>
                  setDraft({ ...draft, invoice_name: e.target.value })
                }
                placeholder="CalPortland"
              />
            </div>
            <div className="form-row">
              <label className="form-label">Name in BuilderTrend</label>
              <input
                className="input"
                value={draft.bt_name}
                onChange={(e) => setDraft({ ...draft, bt_name: e.target.value })}
                placeholder="Catalina Pacific"
              />
              <div className="form-help">
                Must already exist in BuilderTrend. Never a record marked
                &ldquo;DO NOT SELECT&rdquo;.
              </div>
            </div>
            <div className="form-row">
              <label className="form-label">Drive folder name</label>
              <input
                className="input"
                value={draft.drive_folder_name}
                onChange={(e) =>
                  setDraft({ ...draft, drive_folder_name: e.target.value })
                }
                placeholder="Holiday Rock"
              />
              <div className="form-help">
                Exactly as it appears on Drive, misspellings included.
              </div>
            </div>
            <div className="form-row">
              <label className="form-label">Default cost code</label>
              <select
                className="input"
                value={draft.default_cost_code_id}
                onChange={(e) =>
                  setDraft({ ...draft, default_cost_code_id: e.target.value })
                }
              >
                <option value="">None</option>
                {codes.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.code}
                  </option>
                ))}
              </select>
              <div className="form-help">
                Used for non-concrete vendors unless the invoice says
                otherwise.
              </div>
            </div>
          </div>

          <div className="form-checkbox">
            <input
              id="vendor-concrete"
              type="checkbox"
              checked={draft.is_concrete_supplier}
              onChange={(e) =>
                setDraft({ ...draft, is_concrete_supplier: e.target.checked })
              }
            />
            <label htmlFor="vendor-concrete" style={{ fontSize: 13.5 }}>
              Concrete supplier
              <span
                style={{
                  display: "block",
                  fontSize: 12.5,
                  color: "var(--text-faint)",
                  marginTop: 3,
                }}
              >
                Invoices from this vendor need a confirmed mix design on the
                project before a concrete code is suggested.
              </span>
            </label>
          </div>

          <div className="form-row">
            <label className="form-label">Notes</label>
            <textarea
              className="input"
              rows={2}
              value={draft.notes}
              onChange={(e) => setDraft({ ...draft, notes: e.target.value })}
              placeholder="Quirks worth remembering about this vendor."
            />
          </div>

          <div className="form-actions">
            <button
              className="btn btn-accent"
              disabled={
                busy || !draft.invoice_name.trim() || !draft.bt_name.trim()
              }
              onClick={save}
            >
              {busy ? "Saving…" : "Add vendor"}
            </button>
          </div>
        </div>
      )}

      {vendors === null && (
        <div style={{ color: "var(--text-muted)" }}>Loading…</div>
      )}

      {vendors && (
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>On the invoice</th>
                <th>In BuilderTrend</th>
                <th>Drive folder</th>
                <th>Default code</th>
                <th>Concrete</th>
                <th>Active</th>
              </tr>
            </thead>
            <tbody>
              {vendors.length === 0 && (
                <tr>
                  <td colSpan={6} className="dash-table-empty">
                    No vendors yet. Apply migration 003 or add them here.
                  </td>
                </tr>
              )}
              {vendors.map((v) => (
                <tr key={v.id}>
                  <td>
                    {v.invoice_name}
                    {v.aliases.length > 0 && (
                      <span className="cell-sub">
                        also: {v.aliases.join(", ")}
                      </span>
                    )}
                  </td>
                  <td className="mono">{v.bt_name}</td>
                  <td className="mono">
                    {v.drive_folder_name || "—"}
                    {v.drive_folder_name &&
                      v.drive_folder_name !== v.invoice_name && (
                        <span className="cell-sub">
                          Differs from the letterhead spelling
                        </span>
                      )}
                  </td>
                  <td className="mono">{v.default_cost_code || "—"}</td>
                  <td>
                    {v.is_concrete_supplier ? (
                      <span className="pill pill-blue">Ready mix</span>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td>
                    {canWrite ? (
                      <button
                        className="btn btn-ghost"
                        style={{ padding: "4px 10px", fontSize: 12 }}
                        disabled={busy}
                        onClick={() => patch(v, { active: !v.active })}
                      >
                        {v.active ? "Deactivate" : "Activate"}
                      </button>
                    ) : (
                      <span
                        className={`pill ${v.active ? "pill-green" : "pill-muted"}`}
                      >
                        {v.active ? "Active" : "Inactive"}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ─── Users ────────────────────────────────────────────────────────────

function UsersPanel({
  isAdmin,
  onError,
}: {
  isAdmin: boolean;
  onError: (m: string) => void;
}) {
  const [users, setUsers] = useState<AppUser[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState<{ email: string; name: string; roles: Role[] }>(
    { email: "", name: "", roles: ["viewer"] }
  );

  async function reload() {
    try {
      setUsers(
        await api.get<AppUser[]>("/admin/users?include_deactivated=true")
      );
    } catch (e) {
      onError(formatApiError(e));
    }
  }

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function toggleRole(u: AppUser, role: Role) {
    const next = u.roles.includes(role)
      ? u.roles.filter((r) => r !== role)
      : [...u.roles, role];
    if (next.length === 0) {
      onError(
        "A user needs at least one role. Use Viewer for read-only access."
      );
      return;
    }
    setBusy(true);
    try {
      await api.patch(`/admin/users/${u.id}`, { roles: next });
      await reload();
    } catch (e) {
      onError(formatApiError(e));
    } finally {
      setBusy(false);
    }
  }

  async function toggleActive(u: AppUser) {
    setBusy(true);
    try {
      await api.patch(`/admin/users/${u.id}`, {
        deactivated: !u.deactivated_at,
      });
      await reload();
    } catch (e) {
      onError(formatApiError(e));
    } finally {
      setBusy(false);
    }
  }

  async function addUser() {
    setBusy(true);
    try {
      await api.post("/admin/users", {
        email: draft.email.trim().toLowerCase(),
        name: draft.name.trim() || null,
        roles: draft.roles,
      });
      setDraft({ email: "", name: "", roles: ["viewer"] });
      setAdding(false);
      await reload();
    } catch (e) {
      onError(formatApiError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="glass section-card">
      <div className="section-header">
        <div className="section-title">
          Users {users && `(${users.length})`}
        </div>
        {isAdmin && (
          <button className="btn" onClick={() => setAdding((v) => !v)}>
            {adding ? "Cancel" : "Add a person"}
          </button>
        )}
      </div>

      <div className="callout callout-info">
        <div className="callout-title">Roles</div>
        <ul>
          {ALL_ROLES.map((r) => (
            <li key={r}>
              <strong>{ROLE_LABELS[r]}</strong> — {ROLE_DESCRIPTIONS[r]}
            </li>
          ))}
        </ul>
        A person can hold several. Anyone who signs in with a company Google
        account before being added lands as Viewer and can read but not write
        until an admin grants a working role.
      </div>

      {adding && (
        <div
          style={{
            border: "1px solid var(--border)",
            borderRadius: "var(--radius)",
            padding: 18,
            marginBottom: 20,
          }}
        >
          <div className="form-grid-2">
            <div className="form-row">
              <label className="form-label">Email</label>
              <input
                className="input"
                value={draft.email}
                onChange={(e) => setDraft({ ...draft, email: e.target.value })}
                placeholder="name@ferrocretebuilders.com"
              />
              <div className="form-help">
                Must be the address they sign in to Google with. The row is
                claimed on their first sign-in.
              </div>
            </div>
            <div className="form-row">
              <label className="form-label">Name</label>
              <input
                className="input"
                value={draft.name}
                onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                placeholder="Linda"
              />
            </div>
          </div>
          <div className="form-row">
            <label className="form-label">Roles</label>
            <div className="chip-row">
              {ALL_ROLES.map((r) => (
                <button
                  key={r}
                  type="button"
                  className={`dash-chip ${
                    draft.roles.includes(r) ? "dash-chip-active" : ""
                  }`}
                  onClick={() =>
                    setDraft({
                      ...draft,
                      roles: draft.roles.includes(r)
                        ? draft.roles.filter((x) => x !== r)
                        : [...draft.roles, r],
                    })
                  }
                >
                  {ROLE_LABELS[r]}
                </button>
              ))}
            </div>
          </div>
          <div className="form-actions">
            <button
              className="btn btn-accent"
              disabled={busy || !draft.email.trim() || draft.roles.length === 0}
              onClick={addUser}
            >
              {busy ? "Saving…" : "Add person"}
            </button>
          </div>
        </div>
      )}

      {users === null && (
        <div style={{ color: "var(--text-muted)" }}>Loading…</div>
      )}

      {users && (
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Person</th>
                <th>Roles</th>
                <th>Last sign-in</th>
                <th>Access</th>
              </tr>
            </thead>
            <tbody>
              {users.length === 0 && (
                <tr>
                  <td colSpan={4} className="dash-table-empty">
                    No users yet.
                  </td>
                </tr>
              )}
              {users.map((u) => (
                <tr key={u.id}>
                  <td>
                    {u.name || u.email}
                    <span className="cell-sub">{u.email}</span>
                  </td>
                  <td>
                    <div className="chip-row">
                      {ALL_ROLES.map((r) =>
                        isAdmin ? (
                          <button
                            key={r}
                            type="button"
                            className={`dash-chip ${
                              u.roles.includes(r) ? "dash-chip-active" : ""
                            }`}
                            disabled={busy}
                            onClick={() => toggleRole(u, r)}
                          >
                            {ROLE_LABELS[r]}
                          </button>
                        ) : u.roles.includes(r) ? (
                          <span key={r} className="chip">
                            {ROLE_LABELS[r]}
                          </span>
                        ) : null
                      )}
                    </div>
                  </td>
                  <td className="mono">
                    {u.last_login_at
                      ? new Date(u.last_login_at).toLocaleDateString()
                      : "Never"}
                  </td>
                  <td>
                    {isAdmin ? (
                      <button
                        className="btn btn-ghost"
                        style={{ padding: "4px 10px", fontSize: 12 }}
                        disabled={busy}
                        onClick={() => toggleActive(u)}
                      >
                        {u.deactivated_at ? "Reactivate" : "Deactivate"}
                      </button>
                    ) : (
                      <span
                        className={`pill ${
                          u.deactivated_at ? "pill-red" : "pill-green"
                        }`}
                      >
                        {u.deactivated_at ? "Deactivated" : "Active"}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ─── Distribution list ────────────────────────────────────────────────

function DistributionPanel({
  canWrite,
  onError,
}: {
  canWrite: boolean;
  onError: (m: string) => void;
}) {
  const [entries, setEntries] = useState<DistributionEntry[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");

  async function reload() {
    try {
      setEntries(
        await api.get<DistributionEntry[]>(
          "/admin/distribution-list?active_only=false"
        )
      );
    } catch (e) {
      onError(formatApiError(e));
    }
  }

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function add() {
    setBusy(true);
    try {
      await api.post("/admin/distribution-list", {
        email: email.trim().toLowerCase(),
        name: name.trim() || null,
        active: true,
      });
      setEmail("");
      setName("");
      await reload();
    } catch (e) {
      onError(formatApiError(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove(entry: DistributionEntry) {
    setBusy(true);
    try {
      await api.delete(`/admin/distribution-list/${entry.id}`);
      await reload();
    } catch (e) {
      onError(formatApiError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="glass section-card">
      <div className="section-header">
        <div className="section-title">
          Distribution list {entries && `(${entries.length})`}
        </div>
      </div>

      <div className="callout callout-info">
        <div className="callout-title">Who gets the end-of-day summary</div>
        Sent at 5:30 PM Pacific on weekdays, and only when at least one
        invoice was approved that day. Anyone can be on this list; they do not
        need an account in the app.
      </div>

      {canWrite && (
        <div
          style={{
            display: "flex",
            gap: 10,
            alignItems: "flex-end",
            marginBottom: 20,
            flexWrap: "wrap",
          }}
        >
          <div className="form-row" style={{ marginBottom: 0, flex: "1 1 260px" }}>
            <label className="form-label">Email</label>
            <input
              className="input"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="name@ferrocretebuilders.com"
            />
          </div>
          <div className="form-row" style={{ marginBottom: 0, flex: "1 1 180px" }}>
            <label className="form-label">Name</label>
            <input
              className="input"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Optional"
            />
          </div>
          <button
            className="btn btn-accent"
            disabled={busy || !email.trim()}
            onClick={add}
          >
            Add
          </button>
        </div>
      )}

      {entries === null && (
        <div style={{ color: "var(--text-muted)" }}>Loading…</div>
      )}

      {entries && (
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Email</th>
                <th>Name</th>
                <th>Status</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {entries.length === 0 && (
                <tr>
                  <td colSpan={4} className="dash-table-empty">
                    Nobody on the list yet. The end-of-day summary will not
                    send until someone is added.
                  </td>
                </tr>
              )}
              {entries.map((e) => (
                <tr key={e.id}>
                  <td className="mono">{e.email}</td>
                  <td>{e.name || "—"}</td>
                  <td>
                    <span
                      className={`pill ${e.active ? "pill-green" : "pill-muted"}`}
                    >
                      {e.active ? "Active" : "Paused"}
                    </span>
                  </td>
                  <td>
                    {canWrite && (
                      <button
                        className="btn btn-ghost"
                        style={{ padding: "4px 10px", fontSize: 12 }}
                        disabled={busy}
                        onClick={() => remove(e)}
                      >
                        Remove
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
