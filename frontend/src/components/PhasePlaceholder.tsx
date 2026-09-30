import Link from "next/link";

/**
 * Honest placeholder for a screen whose backend arrives in a later phase.
 *
 * Deliberately does NOT fetch anything: a screen that shows "0 invoices"
 * because the endpoint does not exist yet is indistinguishable from a screen
 * that shows "0 invoices" because intake is broken. This says which phase
 * builds it and what it will do, so nobody debugs a feature that was never
 * turned on.
 */
export function PhasePlaceholder({
  eyebrow,
  title,
  meta,
  phase,
  summary,
  willDo,
  cta,
}: {
  eyebrow: string;
  title: string;
  meta: string;
  phase: string;
  summary: string;
  willDo: string[];
  cta?: { href: string; label: string };
}) {
  return (
    <>
      <div className="page-header">
        <div className="page-title-block">
          <div className="page-eyebrow">{eyebrow}</div>
          <h1 className="page-title">{title}</h1>
          <div className="page-meta">{meta}</div>
        </div>
      </div>

      <div className="page-content">
        <div className="glass section-card">
          <span className="phase-note">{phase}</span>
          <p
            style={{
              fontSize: 14.5,
              lineHeight: 1.65,
              color: "var(--text-body)",
              maxWidth: "72ch",
              marginBottom: 20,
            }}
          >
            {summary}
          </p>

          <div className="callout callout-info">
            <div className="callout-title">What this screen will do</div>
            <ul>
              {willDo.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </div>

          {cta && (
            <div style={{ marginTop: 18 }}>
              <Link href={cta.href} className="btn btn-accent">
                {cta.label}
              </Link>
            </div>
          )}
        </div>
      </div>
    </>
  );
}
