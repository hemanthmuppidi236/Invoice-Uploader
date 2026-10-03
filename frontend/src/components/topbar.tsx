"use client";

import { useEffect, useRef, useState } from "react";
import Image from "next/image";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { createClient } from "@/lib/supabase/client";
import { resetCurrentUser } from "@/lib/useCurrentUser";
import { useCurrentUser } from "@/lib/useCurrentUser";
import { hasRole } from "@/lib/types";

/**
 * Nav is role-aware: /admin only appears for admin and accountant, and
 * /uploads only for the people who can actually start a Chrome session
 * (prompt §3). A PE seeing a link they cannot use is a support ticket.
 */
const NAV = [
  { href: "/invoices", label: "Invoices", roles: null },
  { href: "/flagged", label: "Flagged", roles: null },
  { href: "/projects", label: "Projects", roles: null },
  { href: "/uploads", label: "Uploads", roles: ["admin", "accountant"] },
  { href: "/metrics", label: "Metrics", roles: ["admin", "accountant"] },
  { href: "/admin", label: "Admin", roles: ["admin", "accountant"] },
] as const;

export function Topbar({
  email,
  firstName,
  initials,
}: {
  email: string;
  firstName: string;
  initials: string;
}) {
  const pathname = usePathname() || "";
  const router = useRouter();
  const { user } = useCurrentUser();
  const [theme, setTheme] = useState<"light" | "dark">("light");
  const [menuOpen, setMenuOpen] = useState(false);
  const userBlockRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const stored =
      (localStorage.getItem("theme") as "light" | "dark" | null) || "light";
    setTheme(stored);
    document.documentElement.dataset.theme = stored;
    document.body.dataset.theme = stored;
  }, []);

  useEffect(() => {
    if (!menuOpen) return;
    function handleClick(e: MouseEvent) {
      if (
        userBlockRef.current &&
        !userBlockRef.current.contains(e.target as Node)
      ) {
        setMenuOpen(false);
      }
    }
    document.addEventListener("click", handleClick);
    return () => document.removeEventListener("click", handleClick);
  }, [menuOpen]);

  function toggleTheme() {
    const next = theme === "light" ? "dark" : "light";
    setTheme(next);
    document.documentElement.dataset.theme = next;
    document.body.dataset.theme = next;
    localStorage.setItem("theme", next);
  }

  async function signOut() {
    const supabase = createClient();
    await supabase.auth.signOut();
    resetCurrentUser();
    router.push("/login");
    router.refresh();
  }

  const segments = pathname.split("/").filter(Boolean);
  const root = segments[0];

  let crumbs: React.ReactNode;
  if (root === "invoices") {
    crumbs =
      segments.length > 1 ? (
        <>
          <Link href="/invoices" className="breadcrumb-link">
            Invoices
          </Link>
          <span className="breadcrumb-sep">/</span>
          <span className="breadcrumb-active">Invoice</span>
        </>
      ) : (
        <span className="breadcrumb-active">Invoices</span>
      );
  } else if (root === "flagged") {
    crumbs = <span className="breadcrumb-active">Flagged</span>;
  } else if (root === "uploads") {
    crumbs = <span className="breadcrumb-active">Uploads</span>;
  } else if (root === "metrics") {
    crumbs = <span className="breadcrumb-active">AI accuracy</span>;
  } else if (root === "admin") {
    crumbs = <span className="breadcrumb-active">Reference data</span>;
  } else if (root === "projects") {
    if (segments.length === 1) {
      crumbs = <span className="breadcrumb-active">Projects</span>;
    } else if (segments[1] === "new") {
      crumbs = (
        <>
          <Link href="/projects" className="breadcrumb-link">
            Projects
          </Link>
          <span className="breadcrumb-sep">/</span>
          <span className="breadcrumb-active">Onboard a project</span>
        </>
      );
    } else {
      crumbs = (
        <>
          <Link href="/projects" className="breadcrumb-link">
            Projects
          </Link>
          <span className="breadcrumb-sep">/</span>
          <span className="breadcrumb-active">Project</span>
        </>
      );
    }
  } else {
    crumbs = <span className="breadcrumb-active">Invoices</span>;
  }

  const visibleNav = NAV.filter(
    (item) => !item.roles || hasRole(user, ...item.roles)
  );

  return (
    <header className="topbar">
      <div className="topbar-left">
        <Link href="/invoices" aria-label="Home">
          <Image
            src="/logo_white.png"
            alt="Ferrocrete Builders, Inc."
            width={170}
            height={38}
            className="topbar-logo"
            priority
          />
        </Link>
        <div className="topbar-sep" />
        <div className="breadcrumb">{crumbs}</div>
      </div>

      <div className="topbar-right">
        {visibleNav.map((item) => (
          <Link
            key={item.href}
            href={item.href}
            className={`nav-pill ${
              root === item.href.slice(1) ? "active" : ""
            }`}
          >
            {item.label}
          </Link>
        ))}

        <button
          className="theme-toggle"
          onClick={toggleTheme}
          aria-label={`Switch to ${theme === "light" ? "dark" : "light"} mode`}
        >
          {theme === "light" ? "◐" : "☾"}
        </button>

        <div
          className="topbar-user"
          ref={userBlockRef}
          onClick={(e) => {
            e.stopPropagation();
            setMenuOpen((v) => !v);
          }}
        >
          <div className="avatar">{initials}</div>
          <div className="topbar-username">{firstName}</div>

          <div className={`user-menu ${menuOpen ? "open" : ""}`}>
            <div
              className="user-menu-item"
              style={{
                fontSize: 11,
                fontFamily:
                  "IBM Plex Mono, 'Cascadia Mono', Consolas, 'Courier New', ui-monospace, monospace",
                color: "var(--text-faint)",
                letterSpacing: "0.5px",
                cursor: "default",
                pointerEvents: "none",
                textTransform: "lowercase",
              }}
            >
              {email}
            </div>
            {user && user.roles.length > 0 && (
              <div
                className="user-menu-item"
                style={{
                  fontSize: 11,
                  color: "var(--text-faint)",
                  cursor: "default",
                  pointerEvents: "none",
                }}
              >
                {user.roles.join(" · ")}
              </div>
            )}
            <div className="user-menu-divider" />
            <button
              type="button"
              className="user-menu-item"
              onClick={(e) => {
                e.stopPropagation();
                setMenuOpen(false);
                signOut();
              }}
            >
              Sign out
            </button>
          </div>
        </div>
      </div>
    </header>
  );
}
