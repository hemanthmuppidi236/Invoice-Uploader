-- ╔═══════════════════════════════════════════════════════════════════════╗
-- ║  Grant yourself admin — run ONCE, right after your first sign-in.     ║
-- ╚═══════════════════════════════════════════════════════════════════════╝
--
-- Why this exists. The signup trigger in migration 001 gives every new
-- account the `viewer` role, which is correct: a stranger who signs in with
-- a company Google account should not land with write access. But it means
-- the very first person to sign in has no admin either, and /admin is where
-- users and roles are managed. Somebody has to be promoted from outside the
-- app, once.
--
-- `scripts/seed_users.sql.example` is the other route, and the better one
-- once you have everyone's address — it seeds the whole team at once. It is
-- deliberately all-or-nothing (it refuses to apply while any REPLACE-
-- placeholder remains), so it is not the tool for getting yourself in today.
--
-- How to use:
--   1. Sign in to the app with Google at least once. That creates your row.
--   2. Replace the address below with the one you signed in as.
--   3. Run this in the Supabase SQL editor.
--   4. Reload the app. The Admin tab appears.
--
-- Safe to re-run. It only ever grants; it never removes a role.

DO $$
DECLARE
    target_email TEXT := 'REPLACE-you@ferrocretebuilders.com';
    hit          INTEGER;
BEGIN
    IF target_email LIKE 'REPLACE-%' THEN
        RAISE EXCEPTION
            'Edit target_email first — set it to the address you signed in as.';
    END IF;

    UPDATE app_users
       -- Union rather than overwrite: if this row was seeded with other
       -- roles, promoting should not silently take them away.
       SET role = ARRAY(
               SELECT DISTINCT unnest(role || ARRAY['admin']::TEXT[])
           )
     WHERE lower(email) = lower(target_email);

    GET DIAGNOSTICS hit = ROW_COUNT;

    IF hit = 0 THEN
        -- Not a typo you want to discover later, when the Admin tab still
        -- is not there and nothing says why.
        RAISE EXCEPTION
            'No app_users row for %. Sign in to the app with that Google '
            'account first — the signup trigger creates the row. Check the '
            'exact address with: SELECT email FROM app_users;', target_email;
    END IF;

    RAISE NOTICE 'Granted admin to %.', target_email;
END $$;

-- Confirm.
SELECT email, name, role, created_at
  FROM app_users
 ORDER BY created_at;
