-- Read-only check that migrations 001 through 005 all landed.
-- Run after applying them. Every row should say OK.

SELECT 'tables'         AS what,
       count(*)::text   AS actual,
       '12'             AS expected,
       CASE WHEN count(*) = 12 THEN 'OK' ELSE 'MISSING' END AS status
  FROM pg_tables
 WHERE schemaname = 'public'
   AND tablename IN (
        'app_users', 'cost_codes', 'vendors', 'projects', 'mix_designs',
        'invoices', 'invoice_lines', 'invoice_suggestions', 'invoice_costs',
        'audit_log', 'email_log', 'distribution_list')

UNION ALL
SELECT 'cost codes seeded', count(*)::text, '236',
       CASE WHEN count(*) = 236 THEN 'OK' ELSE 'RUN 002' END
  FROM cost_codes

UNION ALL
SELECT 'cost codes active', count(*)::text, '229',
       CASE WHEN count(*) = 229 THEN 'OK' ELSE 'CHECK 002' END
  FROM cost_codes WHERE active

UNION ALL
SELECT 'vendors seeded', count(*)::text, '6',
       CASE WHEN count(*) = 6 THEN 'OK' ELSE 'RUN 003' END
  FROM vendors

UNION ALL
SELECT 'White Cap default code',
       coalesce(max(c.code), 'none'), '3015 - Hardware & Misc',
       CASE WHEN max(c.code) = '3015 - Hardware & Misc'
            THEN 'OK' ELSE 'RUN 003 AFTER 002' END
  FROM vendors v LEFT JOIN cost_codes c ON c.id = v.default_cost_code_id
 WHERE v.invoice_name = 'White Cap'

UNION ALL
-- The index that failed on the first attempt. Its presence is the signal
-- that 001 ran all the way to the end rather than stopping partway.
SELECT 'send-once index', count(*)::text, '1',
       CASE WHEN count(*) = 1 THEN 'OK' ELSE 'RE-RUN 001' END
  FROM pg_indexes
 WHERE schemaname = 'public' AND indexname = 'idx_email_log_daily_unique'

UNION ALL
SELECT 'signup trigger', count(*)::text, '1',
       CASE WHEN count(*) = 1 THEN 'OK' ELSE 'RE-RUN 001' END
  FROM pg_trigger
 WHERE tgname = 'on_auth_user_created' AND NOT tgisinternal

UNION ALL
-- Migration 004. The unique index is the duplicate-bill guard: one
-- BuilderTrend bill can only ever be recorded against one invoice.
SELECT 'filing columns', count(*)::text, '4',
       CASE WHEN count(*) = 4 THEN 'OK' ELSE 'RUN 004' END
  FROM information_schema.columns
 WHERE table_schema = 'public' AND table_name = 'invoices'
   AND column_name IN ('filed_file_id', 'filing_error',
                       'filing_warnings', 'original_archived')

UNION ALL
SELECT 'bt bill id unique', count(*)::text, '1',
       CASE WHEN count(*) = 1 THEN 'OK' ELSE 'RUN 004' END
  FROM pg_indexes
 WHERE schemaname = 'public' AND indexname = 'idx_invoices_bt_bill_id'

UNION ALL
-- Migration 005. Without it the duplicate check cannot recognise the same
-- PDF arriving twice, which is its only signal that is proof rather than
-- evidence.
SELECT 'pdf hash column', count(*)::text, '1',
       CASE WHEN count(*) = 1 THEN 'OK' ELSE 'RUN 005' END
  FROM information_schema.columns
 WHERE table_schema = 'public' AND table_name = 'invoices'
   AND column_name = 'pdf_sha256'

UNION ALL
SELECT 'storage buckets',
       coalesce(string_agg(name, ', ' ORDER BY name), 'none'),
       'invoices, mix-designs',
       CASE WHEN count(*) = 2 THEN 'OK' ELSE 'CREATE THEM IN STORAGE' END
  FROM storage.buckets
 WHERE name IN ('invoices', 'mix-designs');
