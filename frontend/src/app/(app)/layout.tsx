import { redirect } from "next/navigation";
import { createClient } from "@/lib/supabase/server";
import { Topbar } from "@/components/topbar";

export default async function AppLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();

  if (!user) redirect("/login");

  // Display name and initials come from Google's auth metadata. The ROLE
  // comes from the backend via GET /me — the Supabase session does not
  // carry it (prompt §2).
  const fullName =
    (user.user_metadata?.full_name as string | undefined) ||
    (user.user_metadata?.name as string | undefined) ||
    (user.email ? user.email.split("@")[0] : "User");
  const firstName = fullName.split(" ")[0] || "User";
  const initials =
    fullName
      .split(" ")
      .map((w) => w[0])
      .filter(Boolean)
      .slice(0, 2)
      .join("")
      .toUpperCase() || "U";

  return (
    <>
      <Topbar
        email={user.email || ""}
        firstName={firstName}
        initials={initials}
      />
      {children}
    </>
  );
}
