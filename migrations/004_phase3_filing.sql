-- ─────────────────────────────────────────────────────────────────────────
-- 004 — Phase 3: the BuilderTrend upload and the Drive filing that follows
--
-- Everything the upload itself needs (status values `uploaded` and `filed`,
-- `uploaded_at`, `filed_at`, `bt_bill_id`, `filed_path`) already exists in
-- 001. This migration adds what filing turned out to need once it was
-- written, plus the one index that stops a duplicate bill from being
-- recorded twice.
--
-- Safe to re-run: every statement is IF NOT EXISTS.
-- ─────────────────────────────────────────────────────────────────────────

ALTER TABLE invoices
    ADD COLUMN IF NOT EXISTS filed_file_id     TEXT,
    ADD COLUMN IF NOT EXISTS filing_error      TEXT,
    ADD COLUMN IF NOT EXISTS filing_warnings   TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    ADD COLUMN IF NOT EXISTS original_archived BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN invoices.filed_file_id IS
    'Drive file id of the copy written to BT Invoices/[Project]/[Vendor]/. '
    'Kept so the filed copy can be verified later without a name search — '
    'names are per-folder conventions and can be edited by hand.';
COMMENT ON COLUMN invoices.filing_error IS
    'Why filing failed after a successful BuilderTrend save. An invoice in '
    'status `uploaded` with this set is in BuilderTrend but not yet filed, '
    'which is the state /uploads surfaces for a retry. Cleared on success.';
COMMENT ON COLUMN invoices.filing_warnings IS
    'Non-fatal notes from filing: an empty vendor folder with no convention '
    'to match, a mixed-convention folder, a filename collision that was '
    'suffixed, an original that could not be archived. Worth a human glance, '
    'not worth blocking on.';
COMMENT ON COLUMN invoices.original_archived IS
    'TRUE once the Drive original has been moved to Uploaded/. Separate from '
    'filed_at because the copy and the move fail independently, and only the '
    'copy is worth blocking `filed` on.';

-- ─── The duplicate-bill guard ──────────────────────────────────────────
--
-- SOP §8.4: "Never click Save twice without checking whether the first one
-- fired — that's how duplicate bills get created." The endpoint refuses a
-- second mark-uploaded on an invoice that already has a bill id, but that
-- check alone cannot catch the other direction: the same BuilderTrend bill
-- reported against two different invoice rows, which is what happens when a
-- save lands on the wrong record.
--
-- A BuilderTrend bill id identifies one bill, so it belongs to at most one
-- invoice here. Partial, because every row starts with it NULL.

CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_bt_bill_id
    ON invoices(bt_bill_id)
    WHERE bt_bill_id IS NOT NULL;

-- Drives the /uploads screen: the approved-not-uploaded queue, and the
-- uploaded-not-filed retry list.
CREATE INDEX IF NOT EXISTS idx_invoices_upload_queue
    ON invoices(status, approved_at)
    WHERE status IN ('approved', 'uploaded');
