/**
 * Supabase client for use in client components.
 * Uses the anon key — only respects RLS-protected reads.
 */

import { createBrowserClient } from "@supabase/ssr";

import { supabaseAnonKey, supabaseUrl } from "./env";

export function createClient() {
  return createBrowserClient(
    supabaseUrl(),
    supabaseAnonKey()
  );
}
