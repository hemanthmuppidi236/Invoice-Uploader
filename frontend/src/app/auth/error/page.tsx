import Link from "next/link";

export default async function AuthErrorPage({
  searchParams,
}: {
  searchParams: Promise<{ message?: string }>;
}) {
  const params = await searchParams;
  return (
    <div className="login-screen">
      <div className="login-card glass-strong">
        <div className="login-title">Sign-in failed</div>
        <div className="login-subtitle">Authentication Error</div>

        <p style={{ fontSize: 14, color: "var(--text-body)", marginBottom: 16 }}>
          {params.message || "Something went wrong during authentication."}
        </p>

        <p
          style={{
            fontSize: 12,
            color: "var(--text-faint)",
            fontFamily: "IBM Plex Mono, 'Cascadia Mono', Consolas, 'Courier New', ui-monospace, monospace",
            letterSpacing: "0.4px",
            lineHeight: 1.7,
            marginBottom: 24,
          }}
        >
          Signups are restricted to <b>@ferrocretebuilders.com</b>. If you used a
          different account, sign out of Google and retry with the right one.
        </p>

        <Link href="/login" className="btn btn-accent btn-google">
          Try again
        </Link>
      </div>
    </div>
  );
}
