-- ─────────────────────────────────────────────────────────────────────────
-- 005 — Phase 4: duplicate detection hardening
--
-- The Phase 1 check matched on exact equality: same vendor and invoice
-- number, or same vendor, amount and date. Both are defeated by the most
-- ordinary thing that happens to these documents — a re-scan. OCR reads
-- 2294.25 as 2294.26, or 278461-1 as 2784611, and the second copy sails
-- through as a new payable. A missed duplicate is a vendor paid twice, and
-- it surfaces in a reconciliation weeks later, or not at all.
--
-- Near-amount and normalised-number matching are code. The one thing that
-- needs a column is the strongest signal of all: the same PDF ingested
-- twice. That is not evidence, it is proof, and it catches the case where
-- every extracted field came out different because the scan was worse the
-- second time.
--
-- Safe to re-run.
-- ─────────────────────────────────────────────────────────────────────────

ALTER TABLE invoices
    ADD COLUMN IF NOT EXISTS pdf_sha256 TEXT;

COMMENT ON COLUMN invoices.pdf_sha256 IS
    'SHA-256 of the stored PDF bytes. Set on every intake path — Drive poll, '
    'a split page of a combined file, and a hand upload — so the same '
    'document is recognised however it arrived. A hand upload also encodes '
    'this in source_file_id, but a separate column is what makes a Drive '
    'arrival and an emailed copy of the same invoice comparable.';

-- Supports the duplicate scan, which is always scoped to one vendor: two
-- vendors legitimately use the same invoice numbers, so comparing across
-- them would flag constantly and teach everyone to ignore the flag.
CREATE INDEX IF NOT EXISTS idx_invoices_vendor_sha
    ON invoices(vendor_id, pdf_sha256)
    WHERE pdf_sha256 IS NOT NULL;

-- The scan reads one vendor's recent invoices and scores them in memory,
-- rather than running a query per rule. A combined rule ("the number matches
-- AND the amount is within a cent") cannot be expressed as separate queries.
CREATE INDEX IF NOT EXISTS idx_invoices_vendor_date
    ON invoices(vendor_id, invoice_date DESC)
    WHERE status <> 'void';
