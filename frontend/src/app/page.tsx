import { redirect } from "next/navigation";

/** Root sends everyone to the invoice queue, the default landing (§10). */
export default function Home() {
  redirect("/invoices");
}
