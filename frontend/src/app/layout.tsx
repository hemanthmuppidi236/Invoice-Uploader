import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Ferrocrete Invoice Processor",
  description:
    "Vendor invoice review, cost coding, and approval for Ferrocrete Builders",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
};

// Apply the saved theme before first paint so dark mode does not flash light
// on load. The topbar keeps managing the toggle after hydration.
const THEME_INIT = `(function(){try{var t=localStorage.getItem('theme');if(t!=='dark')t='light';document.documentElement.dataset.theme=t;}catch(e){}})();`;

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT }} />
      </head>
      <body className="app-shell" suppressHydrationWarning>
        {children}
      </body>
    </html>
  );
}
