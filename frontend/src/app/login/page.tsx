"use client";

import { Suspense, useEffect, useState } from "react";
import Image from "next/image";
import { useSearchParams } from "next/navigation";
import { createClient } from "@/lib/supabase/client";

export default function LoginPage() {
  return (
    <Suspense fallback={<LoginFallback />}>
      <LoginCard />
    </Suspense>
  );
}

function LoginFallback() {
  return (
    <div className="login-screen">
      <div className="login-card glass-strong">
        <div style={{ color: "var(--text-muted)" }}>Loading…</div>
      </div>
    </div>
  );
}

function LoginCard() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [logoSrc, setLogoSrc] = useState("/logo_dark.png");
  const params = useSearchParams();
  const next = params.get("next") || "/invoices";

  // The login screen background is light, so use the dark-on-cream logo by default.
  // Swap to white logo if user previously set dark theme.
  useEffect(() => {
    const stored = (localStorage.getItem("theme") as "light" | "dark" | null) || "light";
    setLogoSrc(stored === "dark" ? "/logo_white.png" : "/logo_dark.png");
  }, []);

  async function signInWithGoogle() {
    setLoading(true);
    setError(null);
    const supabase = createClient();
    const { error } = await supabase.auth.signInWithOAuth({
      provider: "google",
      options: {
        redirectTo: `${window.location.origin}/auth/callback?next=${encodeURIComponent(next)}`,
        queryParams: {
          access_type: "offline",
          prompt: "consent",
        },
      },
    });
    if (error) {
      setError(error.message);
      setLoading(false);
    }
  }

  return (
    <div className="login-screen">
      <div className="login-card glass-strong">
        <Image
          src={logoSrc}
          alt="Ferrocrete Builders, Inc."
          width={220}
          height={42}
          className="login-logo"
          priority
        />
        <div className="login-title">Invoice Processor</div>
        <div className="login-subtitle">Vendor Invoice Review &amp; Cost Coding</div>

        <button
          onClick={signInWithGoogle}
          disabled={loading}
          className="btn btn-google"
          type="button"
        >
          {loading ? (
            <>
              <span className="spinner" /> Authenticating…
            </>
          ) : (
            <>
              <GoogleIcon /> Sign in with Google
            </>
          )}
        </button>

        {error && <div className="login-error">{error}</div>}

        <div className="login-divider" />

        <div className="login-footer">
          <b>Restricted access</b>
          <br />
          Sign in with your @ferrocretebuilders.com account.
        </div>
      </div>
    </div>
  );
}

function GoogleIcon() {
  return (
    <svg viewBox="0 0 18 18" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <path
        d="M17.64 9.205c0-.639-.057-1.252-.164-1.841H9v3.481h4.844a4.14 4.14 0 0 1-1.796 2.716v2.259h2.908c1.702-1.567 2.684-3.875 2.684-6.615z"
        fill="#4285F4"
      />
      <path
        d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.96v2.332A8.997 8.997 0 0 0 9 18z"
        fill="#34A853"
      />
      <path
        d="M3.964 10.71A5.41 5.41 0 0 1 3.682 9c0-.593.102-1.17.282-1.71V4.958H.96A8.996 8.996 0 0 0 0 9c0 1.452.348 2.827.96 4.042l3.004-2.332z"
        fill="#FBBC05"
      />
      <path
        d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0A8.997 8.997 0 0 0 .96 4.958L3.964 7.29C4.672 5.163 6.656 3.58 9 3.58z"
        fill="#EA4335"
      />
    </svg>
  );
}
