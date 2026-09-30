"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { CurrentUser } from "@/lib/types";

/**
 * Fetch the current user from the backend (which knows the role,
 * unlike Supabase auth which only has email + name).
 *
 * Cached in module scope so multiple components on the same page
 * don't re-fetch.
 */
let cached: CurrentUser | null = null;
let cachedPromise: Promise<CurrentUser> | null = null;

export function useCurrentUser() {
  const [user, setUser] = useState<CurrentUser | null>(cached);
  const [loading, setLoading] = useState(!cached);

  useEffect(() => {
    if (cached) {
      setUser(cached);
      setLoading(false);
      return;
    }
    if (!cachedPromise) {
      cachedPromise = api.get<CurrentUser>("/me").then((u) => {
        cached = u;
        return u;
      });
    }
    cachedPromise
      .then((u) => {
        setUser(u);
        setLoading(false);
      })
      .catch(() => {
        setLoading(false);
      });
  }, []);

  return { user, loading };
}

/** Reset the cached user — call on sign out. */
export function resetCurrentUser() {
  cached = null;
  cachedPromise = null;
}
