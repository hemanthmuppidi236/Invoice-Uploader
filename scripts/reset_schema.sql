-- ╔═══════════════════════════════════════════════════════════════════════╗
-- ║  DESTRUCTIVE — drops every table this app owns, and all their data.    ║
-- ║                                                                        ║
-- ║  For use during initial setup only, when a migration failed partway    ║
-- ║  through and you want a clean slate before re-running 001 to 004.      ║
-- ║                                                                        ║
-- ║  Never run this against a database holding real invoices.              ║
-- ║                                                                        ║
-- ║  Touches only objects created by migration 001. It does not drop the   ║
-- ║  `public` schema itself, so Supabase's own objects are left alone.     ║
-- ╚═══════════════════════════════════════════════════════════════════════╝

-- The trigger on auth.users first: it depends on the function below, and
-- leaving it behind would break every new signup.
DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
DROP TRIGGER IF EXISTS on_auth_user_created_claim ON auth.users;

DROP FUNCTION IF EXISTS public.handle_new_user() CASCADE;
DROP FUNCTION IF EXISTS public.claim_seeded_user() CASCADE;
DROP FUNCTION IF EXISTS public.trigger_set_updated_at() CASCADE;

-- Children before parents. CASCADE covers the foreign keys either way, but
-- the order makes the dependency structure readable.
DROP TABLE IF EXISTS email_log            CASCADE;
DROP TABLE IF EXISTS audit_log            CASCADE;
DROP TABLE IF EXISTS invoice_costs        CASCADE;
DROP TABLE IF EXISTS invoice_suggestions  CASCADE;
DROP TABLE IF EXISTS invoice_lines        CASCADE;
DROP TABLE IF EXISTS invoices             CASCADE;
DROP TABLE IF EXISTS mix_designs          CASCADE;
DROP TABLE IF EXISTS projects             CASCADE;
DROP TABLE IF EXISTS vendors              CASCADE;
DROP TABLE IF EXISTS cost_codes           CASCADE;
DROP TABLE IF EXISTS distribution_list    CASCADE;
DROP TABLE IF EXISTS app_users            CASCADE;

-- Confirm: this should return zero rows.
SELECT tablename
  FROM pg_tables
 WHERE schemaname = 'public'
   AND tablename IN (
        'app_users', 'cost_codes', 'vendors', 'projects', 'mix_designs',
        'invoices', 'invoice_lines', 'invoice_suggestions', 'invoice_costs',
        'audit_log', 'email_log', 'distribution_list'
   );
