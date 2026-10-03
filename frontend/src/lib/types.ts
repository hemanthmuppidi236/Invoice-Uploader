/**
 * Shared types, mirroring the FastAPI schemas.
 *
 * Kept hand-written rather than generated so the field comments explaining
 * the BuilderTrend and SOP semantics live next to the fields they describe.
 */

export type UUID = string;
/** ISO date, "YYYY-MM-DD". */
export type ISODate = string;
/** ISO datetime, UTC. */
export type ISODateTime = string;
/** Money as a number. The backend stores NUMERIC(14,2). */
export type Money = number;

// ─── Roles ────────────────────────────────────────────────────────────

export type Role = "admin" | "accountant" | "approver" | "pe" | "viewer";

export const ALL_ROLES: Role[] = [
  "admin",
  "accountant",
  "approver",
  "pe",
  "viewer",
];

export const ROLE_LABELS: Record<Role, string> = {
  admin: "Admin",
  accountant: "Accounting",
  approver: "Approver",
  pe: "Project engineer",
  viewer: "Viewer",
};

export const ROLE_DESCRIPTIONS: Record<Role, string> = {
  admin: "Everything, plus void, delete, and user management.",
  accountant:
    "Assign and reassign, run intake, trigger uploads, manage reference data.",
  approver: "Approve or reject invoices, view all.",
  pe: "Review invoices assigned to them, edit suggested fields, view all.",
  viewer: "Read only.",
};

export interface CurrentUser {
  id: UUID;
  email: string;
  name: string | null;
  roles: Role[];
}

/** True when the user holds at least one of the named roles. */
export function hasRole(
  user: CurrentUser | null,
  ...roles: Role[]
): boolean {
  if (!user) return false;
  return user.roles.some((r) => roles.includes(r));
}

// ─── Invoices ─────────────────────────────────────────────────────────

/**
 * Invoice status. Only `approved` is read by the Chrome uploader, and
 * nothing reaches BuilderTrend without a human `reviewed` and `approved`
 * stamp (prompt §6, §12).
 */
export type InvoiceStatus =
  | "ingested"
  | "suggested"
  | "assigned"
  | "pending_approval"
  | "approved"
  | "uploaded"
  | "filed"
  | "flagged"
  | "void";

export const INVOICE_STATUS_LABELS: Record<InvoiceStatus, string> = {
  ingested: "Ingested",
  suggested: "Suggested",
  assigned: "In review",
  pending_approval: "Pending approval",
  approved: "Approved",
  uploaded: "Uploaded",
  filed: "Filed",
  flagged: "Flagged",
  void: "Void",
};

/** Triage buckets for /flagged (prompt §7.1, §7.2, §10). */
export type FlagCode =
  | "unknown_vendor"
  | "unknown_project"
  | "new_project"
  | "project_not_onboarded"
  | "possible_duplicate"
  | "unreadable"
  | "yard"
  | "ai_error"
  | "stop_and_ask"
  | "other";

export const FLAG_LABELS: Record<FlagCode, string> = {
  unknown_vendor: "Unknown vendor",
  unknown_project: "Unknown project",
  new_project: "New project, separate workflow",
  project_not_onboarded: "Project not onboarded",
  possible_duplicate: "Possible duplicate",
  unreadable: "Unreadable PDF",
  yard: "Yard invoice, do not enter",
  ai_error: "AI or parse error",
  stop_and_ask: "Chrome session stopped to ask",
  other: "Other",
};

// ─── Cost codes ───────────────────────────────────────────────────────

export interface CostCode {
  id: UUID;
  /** The full BuilderTrend sub-code, e.g. "3002 - Concrete Columns". */
  code: string;
  /** The base number that goes in the Bill's Title field, e.g. "3002". */
  base_code: string | null;
  category: string | null;
  description: string;
  element_keywords: string[];
  active: boolean;
}

// ─── Vendors ──────────────────────────────────────────────────────────

export interface Vendor {
  id: UUID;
  /** The name printed on the letterhead. */
  invoice_name: string;
  /** The name as BuilderTrend spells it (SOP §6). */
  bt_name: string;
  /** The Drive folder name, which sometimes differs from both. */
  drive_folder_name: string | null;
  default_cost_code_id: UUID | null;
  default_cost_code: string | null;
  is_concrete_supplier: boolean;
  aliases: string[];
  notes: string | null;
  active: boolean;
}

// ─── Users ────────────────────────────────────────────────────────────

export interface AppUser {
  id: UUID;
  email: string;
  name: string | null;
  roles: Role[];
  deactivated_at: ISODateTime | null;
  last_login_at: ISODateTime | null;
}

export function userLabel(u: AppUser | null | undefined): string {
  if (!u) return "—";
  return u.name || u.email;
}

// ─── Distribution list ────────────────────────────────────────────────

export interface DistributionEntry {
  id: UUID;
  email: string;
  name: string | null;
  active: boolean;
}

// ─── Mix designs ──────────────────────────────────────────────────────

export interface MixDesignRow {
  id: UUID;
  project_id: UUID;
  revision: string;
  /** The mix number as printed, e.g. "4018045". */
  mix_no: string;
  psi: number | null;
  /** Building elements this mix is approved for. One mix often serves many. */
  element_use: string[];
  /** Pump line size off the mix name, e.g. '3" LINE'. */
  pump_line: string | null;
  cost_code_id: UUID | null;
  cost_code: string | null;
  superseded_at: ISODateTime | null;
  created_at: ISODateTime | null;
}

/** A row as parsed from a submittal, before anyone has confirmed it. */
export interface ParsedMixRow {
  mix_no: string;
  psi: number | null;
  element_use: string[];
  pump_line: string | null;
  cost_code_id: UUID | null;
  proposed_code: string | null;
  confidence: number | null;
  rationale: string | null;
}

export interface MixDesignParseResult {
  storage_path: string;
  original_filename: string;
  revision: string | null;
  project_hint: string | null;
  supplier_hint: string | null;
  rows: ParsedMixRow[];
  /** Things the model wants a human to check by eye. */
  notes: string[];
  /** Things the backend rejected, e.g. an invented cost code. */
  warnings: string[];
  model: string | null;
}

// ─── Projects ─────────────────────────────────────────────────────────

/**
 * Not a construction status. Per prompt §4.1 this marks whether the project
 * belongs to this workflow at all: `old` routes normally, `active` is handled
 * by a separate workflow and its invoices are flagged rather than routed.
 */
export type ProjectScope = "old" | "active";

export const PROJECT_SCOPE_LABELS: Record<ProjectScope, string> = {
  old: "Legacy, in scope",
  active: "New, separate workflow",
};

export interface OnboardingStatus {
  onboarded: boolean;
  /** Empty means this project may be onboarded (or already is). */
  blockers: string[];
  mix_design_rows: number;
  mix_design_rows_mapped: number;
}

export interface Project {
  id: UUID;
  project_no: string;
  name: string;
  bt_job_id: string | null;
  address: string | null;
  pe_user_id: UUID | null;
  pe_name: string | null;
  default_approver_id: UUID | null;
  default_approver_name: string | null;
  status: ProjectScope;
  drive_folder_name: string | null;
  has_concrete_supplier: boolean;
  expected_vendor_ids: UUID[];
  onboarded_at: ISODateTime | null;
  mix_design_pdf_path: string | null;
  quirks: Record<string, unknown>;
  notes: string | null;
  created_at: ISODateTime | null;
  updated_at: ISODateTime | null;
  onboarding: OnboardingStatus | null;
  invoice_count: number;
}

export interface ProjectDetail extends Project {
  mix_designs: MixDesignRow[];
}

export interface SignedUrl {
  url: string;
  expires_in: number;
}

// ─── Confidence display (prompt §8) ───────────────────────────────────

export type ConfidenceBand = "high" | "medium" | "low" | "none";

/**
 * Thresholds from prompt §8: >= 0.85 high (green), 0.6 to 0.85 medium (gold),
 * below 0.6 low (red, reviewer must pick).
 */
export function confidenceBand(c: number | null | undefined): ConfidenceBand {
  if (c === null || c === undefined) return "none";
  if (c >= 0.85) return "high";
  if (c >= 0.6) return "medium";
  return "low";
}

export function confidencePillClass(c: number | null | undefined): string {
  switch (confidenceBand(c)) {
    case "high":
      return "pill-green";
    case "medium":
      return "pill-amber";
    case "low":
      return "pill-red";
    default:
      return "pill-muted";
  }
}

export function confidenceLabel(c: number | null | undefined): string {
  if (c === null || c === undefined) return "No suggestion";
  return `${Math.round(c * 100)}%`;
}

// ─── Invoices ─────────────────────────────────────────────────────────

export interface InvoiceLine {
  id: UUID;
  ticket_no: string | null;
  /** The mix number as printed on the line. The §8.2 cost-code signal. */
  prod_num: string | null;
  description: string | null;
  qty: number | null;
  uom: string | null;
  unit_price: number | null;
  gross: number | null;
  sort_order: number;
}

export interface InvoiceCost {
  id: UUID;
  cost_code_id: UUID;
  /** Full sub-code, what goes in the BuilderTrend Costs row. */
  code: string | null;
  /** Base number, what goes in the BuilderTrend Bill Title field. */
  base_code: string | null;
  amount: Money;
  sort_order: number;
}

export interface SuggestionAlternative {
  cost_code_id: UUID;
  code: string;
  why: string;
}

export interface Suggestion {
  id: UUID;
  cost_code_id: UUID | null;
  code: string | null;
  confidence: number | null;
  rationale: string | null;
  alternatives: SuggestionAlternative[];
  /** The whole model response, never mutated. */
  extracted: Record<string, unknown>;
  model: string | null;
  created_at: ISODateTime | null;
}

export interface AuditEntry {
  id: UUID;
  action: string;
  from_status: string | null;
  to_status: string | null;
  actor_id: UUID | null;
  actor_name: string | null;
  /** Set when there is no user behind the action, e.g. "chrome-agent". */
  actor_label: string | null;
  diff: Record<string, unknown> | null;
  at: ISODateTime;
}

export interface Invoice {
  id: UUID;
  project_id: UUID | null;
  project_no: string | null;
  project_name: string | null;
  vendor_id: UUID | null;
  vendor_name: string | null;
  vendor_bt_name: string | null;

  invoice_no: string | null;
  /** Last 4 digits of invoice_no (SOP §4). */
  bill_no: string | null;
  invoice_date: ISODate | null;
  /** End of the month after invoice_date (SOP §4). */
  due_date: ISODate | null;
  amount: Money | null;
  is_credit: boolean;

  status: InvoiceStatus;
  reviewer_id: UUID | null;
  reviewer_name: string | null;
  approver_id: UUID | null;
  approver_name: string | null;

  assigned_at: ISODateTime | null;
  reviewed_at: ISODateTime | null;
  approved_at: ISODateTime | null;
  rejected_at: ISODateTime | null;
  reject_reason: string | null;
  uploaded_at: ISODateTime | null;
  filed_at: ISODateTime | null;

  flagged_at: ISODateTime | null;
  flag_code: FlagCode | null;
  flag_detail: string | null;
  status_before_flag: string | null;
  duplicate_of_invoice_id: UUID | null;

  bt_bill_id: string | null;
  filed_path: string | null;
  filed_file_id: string | null;
  /** The Drive original has been moved to `Uploaded/` (prompt §7.7). */
  original_archived: boolean;
  /** Set when the bill saved but the Drive filing did not. Retryable. */
  filing_error: string | null;
  filing_warnings: string[];
  void_reason: string | null;

  source_filename: string | null;
  source_path: string | null;
  source_file_id: string | null;
  source_page: number;
  pdf_storage_path: string | null;

  created_at: ISODateTime | null;
  updated_at: ISODateTime | null;

  suggested_code: string | null;
  suggested_confidence: number | null;
  /** Cost rows must sum to `amount` before approval (prompt §5). */
  costs_balanced: boolean;
  costs_difference: Money | null;
  age_days: number | null;
}

export interface InvoiceDetail extends Invoice {
  lines: InvoiceLine[];
  costs: InvoiceCost[];
  suggestion: Suggestion | null;
  audit: AuditEntry[];
  duplicate_of: Invoice | null;
}

export interface InvoiceStats {
  unassigned: number;
  in_review: number;
  pending_approval: number;
  approved_not_uploaded: number;
  flagged: number;
  total_open: number;
  flagged_by_reason: Partial<Record<FlagCode, number>>;
}

export interface InvoiceDashboard {
  invoices: Invoice[];
  stats: InvoiceStats;
  your_court: Invoice[];
  your_court_label: string | null;
}

export interface PollResult {
  scanned: number;
  ingested: number;
  suggested: number;
  flagged: number;
  skipped_existing: number;
  errors: string[];
  detail: { file?: string; outcome?: string; detail?: string; invoice_id?: string }[];
}

// ─── Display helpers ──────────────────────────────────────────────────

export function statusPillClass(status: InvoiceStatus): string {
  switch (status) {
    case "ingested":
      return "pill-muted";
    case "suggested":
      return "pill-blue";
    case "assigned":
      return "pill-amber";
    case "pending_approval":
      return "pill-amber";
    case "approved":
      return "pill-blue";
    case "uploaded":
      return "pill-green";
    case "filed":
      return "pill-green";
    case "flagged":
      return "pill-red";
    case "void":
      return "pill-muted";
    default:
      return "pill-muted";
  }
}

/** "$2,294.25", or "($500.00)" for a credit — accounting convention. */
export function fmtMoney(n: Money | null | undefined): string {
  if (n === null || n === undefined) return "—";
  const abs = Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return n < 0 ? `($${abs})` : `$${abs}`;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** ISO date → "Sep 2" (current year) or "Sep 2, 2025". */
export function fmtDate(iso: ISODate | null | undefined): string {
  if (!iso) return "—";
  const [y, m, d] = iso.split("-").map((p) => parseInt(p, 10));
  if (!y || !m || !d) return iso;
  const thisYear = new Date().getFullYear();
  return y === thisYear
    ? `${MONTHS[m - 1]} ${d}`
    : `${MONTHS[m - 1]} ${d}, ${y}`;
}

export function fmtDateTime(iso: ISODateTime | null | undefined): string {
  if (!iso) return "—";
  const dt = new Date(iso);
  if (isNaN(dt.getTime())) return iso;
  return dt.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** Who did a thing, whether that was a person or the Chrome session. */
export function actorLabel(entry: AuditEntry): string {
  if (entry.actor_name) return entry.actor_name;
  if (entry.actor_label === "chrome-agent") return "Chrome session";
  return entry.actor_label || "System";
}

// ─── Workflow (Phase 2) ───────────────────────────────────────────────

export interface BulkAssignResult {
  assigned: number;
  failed: number;
  results: { invoice_id: string; ok: boolean; detail?: string }[];
}

export interface EmailJobResult {
  sent: number;
  skipped: number;
  failed: number;
  recipients: { status: string; recipient: string; error: string | null }[];
  notes: string[];
}

/**
 * Which workflow actions this user can take on this invoice right now.
 *
 * Mirrors the server's guards — `require_role` on the route, plus the
 * record-level checks in state_machine.py. Duplicated deliberately: the
 * client copy decides what to render, the server copy is the guarantee.
 * §10 wants the action panel to render nothing when the role and status
 * combination has no permission, which needs this answered before the click.
 */
export function workflowActions(
  user: CurrentUser | null,
  invoice: Invoice | null
): {
  canAssign: boolean;
  canReassign: boolean;
  canReview: boolean;
  canApprove: boolean;
  /** Why approve is unavailable, when the reason is worth showing. */
  approveBlockedBecause: string | null;
  reviewBlockedBecause: string | null;
} {
  const none = {
    canAssign: false,
    canReassign: false,
    canReview: false,
    canApprove: false,
    approveBlockedBecause: null,
    reviewBlockedBecause: null,
  };
  if (!user || !invoice) return none;

  const isAdmin = hasRole(user, "admin");
  const isAccountant = hasRole(user, "accountant");
  const isApprover = hasRole(user, "approver");
  const isReviewerRole = hasRole(user, "pe", "accountant");

  const canAssign =
    (isAdmin || isAccountant) &&
    ["ingested", "suggested", "flagged"].includes(invoice.status);

  const canReassign =
    (isAdmin || isAccountant) &&
    ["assigned", "pending_approval"].includes(invoice.status);

  // Review: the role says "could be a reviewer", the record says "is this
  // invoice's reviewer". One PE should not certify another's invoice.
  let canReview = false;
  let reviewBlockedBecause: string | null = null;
  if (invoice.status === "assigned" && isReviewerRole) {
    if (isAdmin || isAccountant || invoice.reviewer_id === user.id) {
      // Same precondition list as require_ready_for_review() on the server,
      // in the same order, so the message the reviewer sees before clicking
      // matches the one they would get after.
      const missing: string[] = [];
      if (!invoice.project_id) missing.push("a project");
      if (!invoice.vendor_id) missing.push("a vendor");
      if (invoice.amount === null) missing.push("an amount");
      if (!invoice.invoice_date) missing.push("an invoice date");
      if (!invoice.invoice_no) missing.push("an invoice number");

      if (missing.length > 0) {
        reviewBlockedBecause = `This invoice still needs ${joinWords(missing)}.`;
      } else if (!invoice.approver_id) {
        reviewBlockedBecause =
          "Pick an approver — otherwise nobody is asked to approve it.";
      } else if (!invoice.costs_balanced) {
        reviewBlockedBecause =
          "The cost rows have to sum to the invoice total first.";
      } else {
        canReview = true;
      }
    } else {
      reviewBlockedBecause = "This invoice is assigned to someone else.";
    }
  }

  let canApprove = false;
  let approveBlockedBecause: string | null = null;
  if (invoice.status === "pending_approval") {
    if (isAdmin || (isApprover && invoice.approver_id === user.id)) {
      canApprove = true;
      if (!invoice.costs_balanced) {
        canApprove = false;
        approveBlockedBecause =
          "The cost rows no longer sum to the invoice total. Send it back.";
      }
    } else if (isApprover) {
      approveBlockedBecause =
        "This invoice is waiting on a different approver.";
    }
  }

  return {
    canAssign,
    canReassign,
    canReview,
    canApprove,
    approveBlockedBecause,
    reviewBlockedBecause,
  };
}

function joinWords(items: string[]): string {
  if (items.length === 1) return items[0];
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

// ─── Upload to BuilderTrend (Phase 3, prompt §7.6) ────────────────────

/**
 * One Costs row on the BuilderTrend bill form.
 *
 * SOP §4 is specific: the row's Title is left blank, Qty is 1, and the Unit
 * cost carries the money. So there is no quantity here — a split invoice is
 * several rows, never one row with a quantity.
 */
export interface UploadQueueCost {
  cost_code: string;
  base_code: string | null;
  name: string | null;
  amount: Money;
  note: string | null;
}

/**
 * Everything the Chrome session types, computed server-side.
 *
 * This screen renders it verbatim rather than re-deriving anything, so what
 * Linda checks before starting a session is exactly what the session will
 * enter. A preview that recomputed the values could agree with itself and
 * still disagree with the API.
 */
export interface UploadQueueItem {
  invoice_id: UUID;

  bt_job_id: string | null;
  /** `buildertrend.net/app/Bills/Bill/0/{jobId}` — SOP §8.5. */
  bill_url: string | null;
  project_no: string | null;
  project_name: string | null;

  bill_title: string | null;
  bill_no: string | null;
  pay_to: string | null;
  invoice_no: string | null;
  invoice_date: ISODate | null;
  due_date: ISODate | null;
  amount: Money | null;
  is_credit: boolean;
  costs: UploadQueueCost[];

  pdf_url: string | null;
  pdf_expires_in: number;

  quirks: Record<string, unknown>;
  project_notes: string | null;
  vendor_notes: string | null;
  age_days: number | null;

  /** Proceed, but read this first. */
  warnings: string[];
  /** Do not enter this one. Flag it and move on. */
  blockers: string[];
}

export interface UploadQueue {
  generated_at: ISODateTime;
  count: number;
  queue: UploadQueueItem[];
  blocked: UploadQueueItem[];
  awaiting_filing: Invoice[];
  recently_uploaded: Invoice[];
  filing_by_backend: boolean;
  notes: string[];
}

export interface FilingResult {
  filed: boolean;
  filed_path: string | null;
  original_archived: boolean;
  error: string | null;
  needs_folder: boolean;
  warnings: string[];
}

export interface MarkUploadedResult {
  invoice: InvoiceDetail;
  filing: FilingResult;
  already_recorded: boolean;
}

/**
 * The prompt Linda pastes into the Chrome project to start a session.
 *
 * Kept here rather than in the page body because it is the one piece of text
 * that has to match the saved BuilderTrend process doc word for word — §7.6
 * quotes it, and a paraphrase would send the session looking for a process
 * it cannot find.
 */
export const CHROME_SESSION_PROMPT =
  "Using the saved BuilderTrend invoice upload process, upload the approved invoices from the review app.";

/**
 * The result of handing the app an invoice PDF directly (§7.1, non-Drive).
 *
 * `created` is false when that exact PDF was already in the system — keyed on
 * a hash of the bytes, so a renamed re-send is still recognised. That is a
 * success, but it has to read differently from a new invoice, or a
 * double-click looks like two payables.
 */
export interface UploadResult {
  invoice_id: UUID;
  status: InvoiceStatus;
  created: boolean;
  detail: string | null;
  invoice: InvoiceDetail;
}

// ─── AI accuracy (Phase 4, prompt §12) ────────────────────────────────

export interface MetricsBucket {
  key: string;
  label: string;
  total: number;
  accepted: number;
  /** null, not 0, when there is nothing to judge — 0% would read as the
   *  model failing rather than as no data. */
  rate: number | null;
}

export interface Metrics {
  generated_at: ISODateTime;
  window_days: number;
  judged: number;
  accepted: number;
  rate: number | null;
  by_vendor: MetricsBucket[];
  by_cost_code: MetricsBucket[];
  by_confidence: MetricsBucket[];
  unsuggested: number;
  notes: string[];
}
