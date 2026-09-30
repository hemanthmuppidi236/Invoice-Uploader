-- ╔═══════════════════════════════════════════════════════════════════════╗
-- ║  Migration 003: Seed the vendor mapping                                ║
-- ║                                                                        ║
-- ║  Source: BuilderTrend Invoice Upload SOP §6 plus the vendors named in  ║
-- ║  SOP §5 and §11. `invoice_name` is what the letterhead says (what the  ║
-- ║  AI reads off the PDF); `bt_name` is what Chrome must type into the    ║
-- ║  "Pay to" field. They differ often enough that guessing is the single  ║
-- ║  most common cause of a stalled upload.                                ║
-- ║                                                                        ║
-- ║  Editable from /admin afterward — a new vendor never needs a deploy.   ║
-- ║                                                                        ║
-- ║  NOT SEEDED HERE, because the §15 kickoff data has not been provided:  ║
-- ║    - app_users   (see scripts/seed_users.sql.example)                  ║
-- ║    - projects    (onboard via /projects/new)                           ║
-- ║    - mix_designs (parsed from each project's submittal at onboarding)  ║
-- ║    - distribution_list (add from /admin)                               ║
-- ╚═══════════════════════════════════════════════════════════════════════╝

INSERT INTO vendors (invoice_name, bt_name, drive_folder_name, is_concrete_supplier, aliases, notes)
VALUES
    -- SOP §6: both of these ready-mix letterheads map to one BuilderTrend vendor.
    ('CalPortland', 'Catalina Pacific', 'CalPortland', TRUE,
     ARRAY['Cal Portland', 'CalPortland Company', 'CPC']::TEXT[],
     'SOP §6: letterhead says CalPortland, BuilderTrend vendor is Catalina Pacific.'),

    ('Superior Ready Mix', 'Catalina Pacific', 'Superior Ready Mix', TRUE,
     ARRAY['Superior Ready-Mix', 'Superior RM']::TEXT[],
     'SOP §6: maps to the same BuilderTrend vendor as CalPortland.'),

    -- SOP §5: combined multi-job PDFs, always cost code 3015, and the vendor
    -- search string needs the space ("White Cap", not "WhiteCap").
    ('White Cap', 'White Cap Construction & Industrial Supply', 'White Cap', FALSE,
     ARRAY['White Cap Supply', 'WhiteCap', 'White Cap Construction Supply']::TEXT[],
     'SOP §5: combined file, one page per invoice, always 3015. Search "White Cap" '
     'WITH a space in the BuilderTrend vendor picker. Yard invoices (EL CAJON YARD, '
     'SD Yard) are never entered.'),

    -- SOP §6: no space after the comma in the BuilderTrend record.
    ('Cefali & Associates', 'Cefali & Associates,Inc', 'Cefali & Associates', FALSE,
     ARRAY['Cefali and Associates', 'Cefali']::TEXT[],
     'SOP §6: BuilderTrend spells it "Cefali & Associates,Inc" with no space '
     'after the comma. Concrete testing and inspection consultant.'),

    -- SOP §6: the Drive folder is misspelled and must stay misspelled, because
    -- filing looks the folder up by name and never creates one (SOP §7, §12).
    ('Holliday Rock', 'Holliday Rock', 'Holiday Rock', TRUE,
     ARRAY['Holliday Rock Co', 'Holliday Rock Company']::TEXT[],
     'SOP §6: the Drive folder is spelled "Holiday Rock" (one L). Filing must use '
     'the folder name as it exists on Drive, not the letterhead spelling.'),

    -- SOP §11: first invoice ever processed through this workflow.
    ('Van Matre', 'Van Matre', 'Van Matre', FALSE,
     ARRAY['Van Matre Construction']::TEXT[],
     'SOP §11: batch 1 on 9/8. bt_name unverified against BuilderTrend — confirm '
     'on the next invoice from this vendor.')
ON CONFLICT (invoice_name) DO UPDATE SET
    bt_name              = EXCLUDED.bt_name,
    drive_folder_name    = EXCLUDED.drive_folder_name,
    is_concrete_supplier = EXCLUDED.is_concrete_supplier,
    aliases              = EXCLUDED.aliases,
    notes                = EXCLUDED.notes,
    updated_at           = NOW();


-- White Cap is always "3015 - Hardware & Misc" (SOP §5). Wire the default so
-- the AI never has to reason about it — prompt §8.4 makes the vendor default
-- win for non-concrete vendors unless the invoice says otherwise.
UPDATE vendors
   SET default_cost_code_id = (
           SELECT id FROM cost_codes WHERE code = '3015 - Hardware & Misc'
       )
 WHERE invoice_name = 'White Cap';

-- Cefali is testing and inspection, which is "3001 - Concrete Consultants &
-- Engineering" rather than a ready-mix code.
UPDATE vendors
   SET default_cost_code_id = (
           SELECT id FROM cost_codes WHERE code = '3001 - Concrete Consultants & Engineering'
       )
 WHERE invoice_name = 'Cefali & Associates';


-- Fail loudly if either default failed to resolve. A silently NULL default
-- would send every White Cap invoice to the reviewer with no suggestion,
-- which looks like an AI failure rather than a bad migration.
DO $$
DECLARE
    missing INTEGER;
BEGIN
    SELECT COUNT(*) INTO missing
      FROM vendors
     WHERE invoice_name IN ('White Cap', 'Cefali & Associates')
       AND default_cost_code_id IS NULL;

    IF missing > 0 THEN
        RAISE EXCEPTION
            'Migration 003: % vendor default cost code(s) did not resolve. '
            'Apply migration 002 (cost code seed) first.', missing;
    END IF;
END $$;
