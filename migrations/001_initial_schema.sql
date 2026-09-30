-- ╔═══════════════════════════════════════════════════════════════════════╗
-- ║  FERROCRETE INVOICE PROCESSOR — DATABASE SCHEMA                       ║
-- ║  Migration 001: Initial schema                                        ║
-- ║                                                                       ║
-- ║  Run against: Supabase Postgres (new project, separate from pay app)  ║
-- ║  Conventions (same as the pay app):                                   ║
-- ║    - All ids are UUIDs                                                ║
-- ║    - All money in NUMERIC(14,2) (no floats for currency)              ║
-- ║    - All timestamps in TIMESTAMPTZ, stamped in UTC                    ║
-- ║    - audit_log captures every state transition and every human edit   ║
-- ║    - Writes go through FastAPI with the service-role key; the browser  ║
-- ║      gets read-only RLS policies and no write policies at all         ║
-- ╚═══════════════════════════════════════════════════════════════════════╝

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";


-- ─── USERS ─────────────────────────────────────────────────────────────
-- Mirrors auth.users (Supabase) with app-specific roles + profile data.
-- A trigger keeps this in sync with auth.users on signup.
--
-- DIFFERS FROM THE PAY APP: `role` is an ARRAY here, because prompt §3
-- requires a person to hold more than one role at once (Linda is both
-- accountant and approver). require_role() in app/core/auth.py checks for
-- array intersection rather than equality.

CREATE TABLE app_users (
    id              UUID PRIMARY KEY,        -- matches auth.users.id
    email           TEXT NOT NULL UNIQUE,
    name            TEXT,
    role            TEXT[] NOT NULL DEFAULT ARRAY['viewer']::TEXT[],
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at   TIMESTAMPTZ,
    deactivated_at  TIMESTAMPTZ,

    -- Every element must be a known role, and nobody may hold zero roles.
    CONSTRAINT app_users_role_valid CHECK (
        role <@ ARRAY['admin', 'accountant', 'approver', 'pe', 'viewer']::TEXT[]
        AND COALESCE(array_length(role, 1), 0) >= 1
    )
);

CREATE INDEX idx_app_users_email ON app_users(email);
CREATE INDEX idx_app_users_role ON app_users USING GIN(role);

COMMENT ON COLUMN app_users.role IS
    'Array of roles. New signups default to viewer (read-only) and must be '
    'granted a working role from /admin — an unrecognized @ferrocretebuilders.com '
    'signup should not be able to write anything.';


-- ─── COST CODES ────────────────────────────────────────────────────────
-- Seeded from BuilderTrend''s cost code export (migration 002). Editable
-- from /admin so a BuilderTrend change never needs a code deploy.
--
-- BuilderTrend semantics (SOP §4): the Bill''s "Title" field takes the BASE
-- code (e.g. "3002") while each Costs row takes the full sub-code
-- (e.g. "3002 - Concrete Columns"). Both live here: `base_code` and `code`.

CREATE TABLE cost_codes (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    code                TEXT NOT NULL UNIQUE,   -- '3002 - Concrete Columns'
    base_code           TEXT,                   -- '3002'  (NULL for non-numeric codes)
    category            TEXT,                   -- '3002 - Concrete Ready Mix' (BT CostCategory)
    description         TEXT NOT NULL,          -- 'Concrete Columns'
    element_keywords    TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    active              BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_cost_codes_base ON cost_codes(base_code) WHERE active;
CREATE INDEX idx_cost_codes_keywords ON cost_codes USING GIN(element_keywords);

COMMENT ON COLUMN cost_codes.element_keywords IS
    'Building-element terms that map to this code (columns, shear wall, SOG, '
    'footing, elevated deck, ...). This is what the AI matches mix-design '
    'element uses and invoice line descriptions against (prompt §8.2/§8.3).';


-- ─── VENDORS ───────────────────────────────────────────────────────────
-- Letterhead name vs BuilderTrend name (SOP §6). The letterhead name is what
-- the AI reads off the PDF; bt_name is what Chrome types into "Pay to".

CREATE TABLE vendors (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    invoice_name            TEXT NOT NULL UNIQUE,   -- 'CalPortland'
    bt_name                 TEXT NOT NULL,          -- 'Catalina Pacific'
    drive_folder_name       TEXT,                   -- 'Holiday Rock' (misspelled on Drive)
    default_cost_code_id    UUID REFERENCES cost_codes(id),
    is_concrete_supplier    BOOLEAN NOT NULL DEFAULT FALSE,
    aliases                 TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    notes                   TEXT,
    active                  BOOLEAN NOT NULL DEFAULT TRUE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_vendors_active ON vendors(active);

COMMENT ON COLUMN vendors.default_cost_code_id IS
    'Fallback code for non-concrete vendors (prompt §8.4). White Cap points '
    'at "3015 - Hardware & Misc" and that wins unless the invoice says otherwise.';
COMMENT ON COLUMN vendors.is_concrete_supplier IS
    'TRUE means invoices from this vendor require the project to have a '
    'confirmed mix design before the AI will suggest a concrete cost code.';


-- ─── PROJECTS ──────────────────────────────────────────────────────────
-- `status` here is NOT a construction status. Per prompt §4.1 it marks
-- whether the project belongs to this workflow:
--   'old'    — legacy job, in scope, invoices route normally
--   'active' — new job handled by a separate workflow; its invoices are
--              flagged with reason `new_project` and never routed
--
-- A project is eligible for routing only once `onboarded_at` is stamped
-- (prompt §7.0), which requires a PE and — for concrete jobs — a confirmed
-- mix design.

CREATE TABLE projects (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_no              TEXT NOT NULL UNIQUE,   -- '25-20'
    name                    TEXT NOT NULL,          -- BuilderTrend job name
    bt_job_id               TEXT,                   -- BuilderTrend jobId, used in the Bill URL
    address                 TEXT,
    pe_user_id              UUID REFERENCES app_users(id) ON UPDATE CASCADE,
    default_approver_id     UUID REFERENCES app_users(id) ON UPDATE CASCADE,
    status                  TEXT NOT NULL DEFAULT 'old'
                            CHECK (status IN ('old', 'active')),
    drive_folder_name       TEXT,                   -- folder under BT Invoices/
    has_concrete_supplier   BOOLEAN NOT NULL DEFAULT TRUE,
    expected_vendor_ids     UUID[] NOT NULL DEFAULT ARRAY[]::UUID[],
    onboarded_at            TIMESTAMPTZ,
    onboarded_by            UUID REFERENCES app_users(id) ON UPDATE CASCADE,
    mix_design_pdf_path     TEXT,                   -- Storage path in the mix-designs bucket
    quirks                  JSONB NOT NULL DEFAULT '{}'::JSONB,
    notes                   TEXT,

    created_by              UUID REFERENCES app_users(id) ON UPDATE CASCADE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at              TIMESTAMPTZ
);

CREATE INDEX idx_projects_status ON projects(status) WHERE deleted_at IS NULL;
CREATE INDEX idx_projects_drive_folder ON projects(drive_folder_name) WHERE deleted_at IS NULL;
CREATE INDEX idx_projects_pe ON projects(pe_user_id);

COMMENT ON COLUMN projects.quirks IS
    'Per-project gotchas the Chrome uploader should read before driving '
    'BuilderTrend (SOP §5 ambiguous-job notes, §8 quirks that only bite '
    'certain jobs). Free-form so new lessons need no migration.';
COMMENT ON COLUMN projects.expected_vendor_ids IS
    'Optional allowlist collected at onboarding. Narrows AI vendor matching; '
    'an empty array means "any vendor".';


-- ─── MIX DESIGNS ───────────────────────────────────────────────────────
-- Parsed from the project''s mix design submittal yardage sheet at onboarding
-- (prompt §7.0). One row per mix number. `element_use` is the list of building
-- elements that mix serves — a single mix commonly serves several, which is
-- exactly the disambiguation problem prompt §8 describes.
--
-- Revisions are versioned, never deleted: `superseded_at` is stamped when a
-- newer submittal arrives, so invoices keep the rows they were scored against.

CREATE TABLE mix_designs (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id      UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    revision        TEXT NOT NULL DEFAULT 'CMD-01',
    mix_no          TEXT NOT NULL,          -- '4018045'
    psi             INTEGER,                -- 4000
    element_use     TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    pump_line       TEXT,                   -- '3" LINE' / '4" LINE'
    cost_code_id    UUID REFERENCES cost_codes(id),
    source_pdf_path TEXT,
    superseded_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- A mix number appears once per revision per project. Two revisions may both
-- carry mix 4018045; only one of them is un-superseded at a time.
CREATE UNIQUE INDEX idx_mix_designs_unique ON mix_designs(project_id, revision, mix_no);
CREATE INDEX idx_mix_designs_live ON mix_designs(project_id, mix_no) WHERE superseded_at IS NULL;

COMMENT ON COLUMN mix_designs.cost_code_id IS
    'Set by the PE (AI proposes it) on the onboarding screen. NULL means the '
    'element-to-code mapping is unconfirmed and the AI must not guess a '
    'concrete code from this mix.';


-- ─── INVOICES ──────────────────────────────────────────────────────────
-- One row per invoice. A White Cap combined PDF produces one row per page,
-- all sharing a source_file_id and distinguished by source_page.

CREATE TABLE invoices (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id              UUID REFERENCES projects(id),
    vendor_id               UUID REFERENCES vendors(id),

    invoice_no              TEXT,
    bill_no                 TEXT,           -- last 4 digits of invoice_no (SOP §4)
    invoice_date            DATE,
    due_date                DATE,           -- end of the month after invoice_date (SOP §4)
    amount                  NUMERIC(14,2),
    is_credit               BOOLEAN NOT NULL DEFAULT FALSE,

    -- Provenance on Google Drive
    source_file_id          TEXT NOT NULL,
    source_page             INTEGER NOT NULL DEFAULT 1,
    source_path             TEXT,
    source_filename         TEXT,
    pdf_storage_path        TEXT,           -- Storage path in the invoices bucket

    status                  TEXT NOT NULL DEFAULT 'ingested'
                            CHECK (status IN (
                                'ingested', 'suggested', 'assigned',
                                'pending_approval', 'approved',
                                'uploaded', 'filed',
                                'flagged', 'void'
                            )),

    reviewer_id             UUID REFERENCES app_users(id) ON UPDATE CASCADE,
    approver_id             UUID REFERENCES app_users(id) ON UPDATE CASCADE,

    assigned_at             TIMESTAMPTZ,
    assigned_by             UUID REFERENCES app_users(id) ON UPDATE CASCADE,
    reviewed_at             TIMESTAMPTZ,
    approved_at             TIMESTAMPTZ,
    rejected_at             TIMESTAMPTZ,
    reject_reason           TEXT,
    uploaded_at             TIMESTAMPTZ,
    filed_at                TIMESTAMPTZ,

    -- Flag state. `flag_code` is the triage bucket /flagged groups by;
    -- `flag_detail` carries free text (an API error message, an ambiguity
    -- note from the Chrome session). `status_before_flag` is what unflag
    -- restores, so flagging is reversible from any state.
    flagged_at              TIMESTAMPTZ,
    flag_code               TEXT CHECK (flag_code IS NULL OR flag_code IN (
                                'unknown_vendor', 'unknown_project', 'new_project',
                                'project_not_onboarded', 'possible_duplicate',
                                'unreadable', 'yard', 'ai_error',
                                'stop_and_ask', 'other'
                            )),
    flag_detail             TEXT,
    status_before_flag      TEXT,
    duplicate_of_invoice_id UUID REFERENCES invoices(id),

    bt_bill_id              TEXT,
    filed_path              TEXT,

    voided_at               TIMESTAMPTZ,
    void_reason             TEXT,

    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Prompt §5: one invoice number per vendor. NULLs compare as distinct in
    -- Postgres, so freshly ingested rows (vendor and invoice_no still unknown)
    -- do not collide with each other.
    CONSTRAINT invoices_vendor_invoice_no_unique UNIQUE (vendor_id, invoice_no)
);

-- Drive polling idempotency (prompt §7.1): re-running the poll never creates
-- a second record for a file, or for a page of a combined file.
CREATE UNIQUE INDEX idx_invoices_source ON invoices(source_file_id, source_page);

CREATE INDEX idx_invoices_status ON invoices(status);
CREATE INDEX idx_invoices_reviewer ON invoices(reviewer_id, status);
CREATE INDEX idx_invoices_approver ON invoices(approver_id, status);
CREATE INDEX idx_invoices_project ON invoices(project_id, status);
CREATE INDEX idx_invoices_vendor ON invoices(vendor_id);
CREATE INDEX idx_invoices_flag ON invoices(flag_code) WHERE status = 'flagged';
-- Supports the §8 phase-inference lookback ("last 30 days of approved
-- invoices on the same project").
CREATE INDEX idx_invoices_approved_recent ON invoices(project_id, approved_at DESC)
    WHERE approved_at IS NOT NULL;
-- Supports the duplicate check on (vendor, amount, invoice_date).
CREATE INDEX idx_invoices_dup_check ON invoices(vendor_id, amount, invoice_date);


-- ─── INVOICE LINES ─────────────────────────────────────────────────────
-- Extracted line items. Read-only evidence for the reviewer; the money that
-- matters for BuilderTrend lives in invoice_costs.

CREATE TABLE invoice_lines (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    invoice_id      UUID NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    ticket_no       TEXT,
    prod_num        TEXT,           -- mix number as printed on the line
    description     TEXT,
    qty             NUMERIC(14,4),
    uom             TEXT,           -- 'CY', 'EA', 'HR'
    unit_price      NUMERIC(14,4),
    gross           NUMERIC(14,2),
    sort_order      INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_invoice_lines_invoice ON invoice_lines(invoice_id, sort_order);


-- ─── INVOICE SUGGESTIONS ───────────────────────────────────────────────
-- Full AI output, kept forever. Reviewer edits are diffed against this in
-- audit_log so we can measure how often the model was right (prompt §12).

CREATE TABLE invoice_suggestions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    invoice_id      UUID NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    cost_code_id    UUID REFERENCES cost_codes(id),
    confidence      NUMERIC(4,3),   -- 0.000 to 1.000
    rationale       TEXT,
    alternatives    JSONB NOT NULL DEFAULT '[]'::JSONB,
    extracted       JSONB NOT NULL DEFAULT '{}'::JSONB,
    model           TEXT,
    prompt_tokens   INTEGER,
    output_tokens   INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_invoice_suggestions_invoice ON invoice_suggestions(invoice_id, created_at DESC);

COMMENT ON COLUMN invoice_suggestions.extracted IS
    'The whole model response as returned (vendor, invoice_no, date, amount, '
    'job_hint, comment_hint, mix_nos, lines, flags). Never mutated.';


-- ─── INVOICE COSTS ─────────────────────────────────────────────────────
-- One row per BuilderTrend Costs row. The reviewer may split one invoice
-- across several cost codes; the split must sum to invoices.amount before
-- approval. That sum check is enforced in the API (a cross-row invariant
-- Postgres cannot express as a CHECK) — see app/api/invoices.py.

CREATE TABLE invoice_costs (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    invoice_id      UUID NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    cost_code_id    UUID NOT NULL REFERENCES cost_codes(id),
    amount          NUMERIC(14,2) NOT NULL,
    sort_order      INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- One row per code per invoice; a split uses distinct codes.
    UNIQUE (invoice_id, cost_code_id)
);

CREATE INDEX idx_invoice_costs_invoice ON invoice_costs(invoice_id, sort_order);
CREATE INDEX idx_invoice_costs_code ON invoice_costs(cost_code_id);


-- ─── AUDIT LOG ─────────────────────────────────────────────────────────
-- Prompt §6: every transition stamps actor + UTC time and writes here.
-- Prompt §7.4: every human field edit is diffed here too.
--
-- Shaped per prompt §5 (invoice_id, actor_id, action, from_status, to_status,
-- diff, at) and generalized with entity_type/entity_id so project onboarding
-- and reference-data edits are auditable through the same table.

CREATE TABLE audit_log (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    entity_type     TEXT NOT NULL,          -- 'invoice', 'project', 'mix_design', 'cost_code', ...
    entity_id       UUID NOT NULL,
    invoice_id      UUID REFERENCES invoices(id) ON DELETE CASCADE,
    actor_id        UUID REFERENCES app_users(id) ON UPDATE CASCADE,
    actor_label     TEXT,                   -- 'chrome-agent' for X-Agent-Key calls
    action          TEXT NOT NULL,          -- 'ingested', 'suggested', 'assigned', 'reviewed', ...
    from_status     TEXT,
    to_status       TEXT,
    diff            JSONB,
    at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_audit_log_entity ON audit_log(entity_type, entity_id, at DESC);
CREATE INDEX idx_audit_log_invoice ON audit_log(invoice_id, at DESC);
CREATE INDEX idx_audit_log_actor ON audit_log(actor_id, at DESC);

COMMENT ON COLUMN audit_log.actor_label IS
    'Set when there is no app_users row behind the action — the Chrome agent '
    'authenticates with X-Agent-Key, not a user token, so actor_id is NULL '
    'and this records who acted.';


-- ─── EMAIL LOG ─────────────────────────────────────────────────────────
-- Prompt §9: log every send. Failures alert the admin, never fail silently.

CREATE TABLE email_log (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    kind            TEXT NOT NULL
                    CHECK (kind IN ('daily_court', 'eod_summary', 'admin_alert')),
    recipient       TEXT NOT NULL,
    subject         TEXT,
    invoice_ids     UUID[] NOT NULL DEFAULT ARRAY[]::UUID[],
    sent_at         TIMESTAMPTZ,
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_email_log_kind ON email_log(kind, created_at DESC);
-- Idempotency guard for the scheduled jobs: one send per kind per recipient
-- per calendar day, so a double cron fire cannot double-mail anyone.
--
-- The day is pinned to UTC explicitly. A bare `created_at::DATE` on a
-- TIMESTAMPTZ is STABLE, not IMMUTABLE — its result depends on the session's
-- TimeZone setting — and Postgres refuses a non-immutable expression in an
-- index. `AT TIME ZONE 'UTC'` fixes the zone as a literal, which makes the
-- whole expression immutable and the index deterministic.
--
-- UTC rather than Pacific is deliberate: both jobs fire well inside a single
-- UTC day (the 3:30 PM digest at 22:30 UTC, the 5:30 PM summary at 00:30 UTC
-- the following day), so a retry minutes later always lands on the same UTC
-- date and is correctly suppressed. Note this means an end-of-day summary is
-- logged under the UTC day AFTER the Pacific day it describes.
CREATE UNIQUE INDEX idx_email_log_daily_unique
    ON email_log(kind, recipient, ((created_at AT TIME ZONE 'UTC')::DATE))
    WHERE error IS NULL;


-- ─── DISTRIBUTION LIST ─────────────────────────────────────────────────
-- Recipients of the end-of-day summary (prompt §4.4, §9.2). Managed from
-- /admin.

CREATE TABLE distribution_list (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email       TEXT NOT NULL UNIQUE,
    name        TEXT,
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ─── TRIGGERS ──────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION trigger_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    t TEXT;
BEGIN
    FOR t IN
        SELECT unnest(ARRAY[
            'cost_codes', 'vendors', 'projects', 'mix_designs',
            'invoices', 'invoice_costs', 'distribution_list'
        ])
    LOOP
        EXECUTE format('
            CREATE TRIGGER set_updated_at
            BEFORE UPDATE ON %I
            FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
        ', t);
    END LOOP;
END $$;


-- Sync auth.users → app_users on signup.
--
-- Two cases:
--   a) Brand new person  → insert as 'viewer' (read everything, write nothing).
--      An admin grants real roles from /admin. This is deliberate: Google
--      OAuth is restricted to the Ferrocrete domain, but "has a company
--      email" is not the same as "may approve a bill".
--   b) Pre-seeded person → a row was created from /admin (or migration 003)
--      before they ever signed in, so it has a placeholder id. Claim it by
--      email and re-point it at the real auth id, preserving the roles that
--      were granted. Without this the email UNIQUE constraint would break
--      their first login. Every FK onto app_users(id) is ON UPDATE CASCADE
--      so the re-point carries through.

CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM public.app_users WHERE email = NEW.email) THEN
        UPDATE public.app_users
           SET id   = NEW.id,
               name = COALESCE(name, NEW.raw_user_meta_data->>'full_name', NEW.email)
         WHERE email = NEW.email;
    ELSE
        INSERT INTO public.app_users (id, email, name, role)
        VALUES (
            NEW.id,
            NEW.email,
            COALESCE(NEW.raw_user_meta_data->>'full_name', NEW.email),
            ARRAY['viewer']::TEXT[]
        )
        ON CONFLICT (id) DO NOTHING;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();


-- ─── ROW LEVEL SECURITY (RLS) ──────────────────────────────────────────
-- Same posture as the pay app: authenticated users may READ, and the browser
-- has NO write policies at all. Every write goes through FastAPI with the
-- service-role key (which bypasses RLS) after require_role has run.

ALTER TABLE app_users           ENABLE ROW LEVEL SECURITY;
ALTER TABLE cost_codes          ENABLE ROW LEVEL SECURITY;
ALTER TABLE vendors             ENABLE ROW LEVEL SECURITY;
ALTER TABLE projects            ENABLE ROW LEVEL SECURITY;
ALTER TABLE mix_designs         ENABLE ROW LEVEL SECURITY;
ALTER TABLE invoices            ENABLE ROW LEVEL SECURITY;
ALTER TABLE invoice_lines       ENABLE ROW LEVEL SECURITY;
ALTER TABLE invoice_suggestions ENABLE ROW LEVEL SECURITY;
ALTER TABLE invoice_costs       ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log           ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_log           ENABLE ROW LEVEL SECURITY;
ALTER TABLE distribution_list   ENABLE ROW LEVEL SECURITY;

CREATE POLICY "authenticated_read_all" ON cost_codes
    FOR SELECT TO authenticated USING (true);
CREATE POLICY "authenticated_read_all" ON vendors
    FOR SELECT TO authenticated USING (true);
CREATE POLICY "authenticated_read_all" ON projects
    FOR SELECT TO authenticated USING (true);
CREATE POLICY "authenticated_read_all" ON mix_designs
    FOR SELECT TO authenticated USING (true);
CREATE POLICY "authenticated_read_all" ON invoices
    FOR SELECT TO authenticated USING (true);
CREATE POLICY "authenticated_read_all" ON invoice_lines
    FOR SELECT TO authenticated USING (true);
CREATE POLICY "authenticated_read_all" ON invoice_suggestions
    FOR SELECT TO authenticated USING (true);
CREATE POLICY "authenticated_read_all" ON invoice_costs
    FOR SELECT TO authenticated USING (true);
CREATE POLICY "authenticated_read_all" ON audit_log
    FOR SELECT TO authenticated USING (true);
CREATE POLICY "authenticated_read_all" ON distribution_list
    FOR SELECT TO authenticated USING (true);

-- app_users: a signed-in person may read their own row. The roster (for the
-- reviewer/approver dropdowns) is served by the backend, not read directly.
CREATE POLICY "authenticated_read_self" ON app_users
    FOR SELECT TO authenticated USING (auth.uid() = id);

-- email_log gets no read policy: it is operational and admin-only via the API.


COMMENT ON SCHEMA public IS
    'Ferrocrete Invoice Processor — initial schema, migration 001';
