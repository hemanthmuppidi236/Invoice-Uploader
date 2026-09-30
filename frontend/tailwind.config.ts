import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./src/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  darkMode: ["class", '[data-theme="dark"]'],
  theme: {
    extend: {
      fontFamily: {
        // Preserves Demo 2 v5 typography
        serif: ["EB Garamond", "Georgia", "serif"],
        display: ["Playfair Display", "Georgia", "serif"],
        mono: ["IBM Plex Mono", "Menlo", "monospace"],
      },
      colors: {
        // Map Tailwind utilities to CSS vars (defined in globals.css).
        // This keeps light/dark switching trivial.
        topbar: {
          DEFAULT: "var(--topbar)",
          text: "var(--topbar-text)",
          muted: "var(--topbar-muted)",
        },
        text: {
          primary: "var(--text-primary)",
          body: "var(--text-body)",
          muted: "var(--text-muted)",
          faint: "var(--text-faint)",
          placeholder: "var(--text-placeholder)",
        },
        accent: {
          DEFAULT: "var(--accent)",
          text: "var(--accent-text)",
          dim: "var(--accent-dim)",
          border: "var(--accent-border)",
        },
        ferrocrete: {
          red: "var(--ferrocrete-red)",
        },
        status: {
          green: "var(--status-green)",
          amber: "var(--status-amber)",
          red: "var(--status-red)",
          blue: "var(--status-blue)",
        },
        glass: {
          bg: "var(--glass-bg)",
          "bg-strong": "var(--glass-bg-strong)",
          border: "var(--glass-border)",
        },
      },
      borderRadius: {
        sm: "var(--radius-sm)",
        DEFAULT: "var(--radius)",
        lg: "var(--radius-lg)",
        pill: "var(--radius-pill)",
      },
      backdropBlur: {
        glass: "var(--blur)",
      },
      transitionTimingFunction: {
        glide: "var(--glide)",
      },
    },
  },
  plugins: [],
};

export default config;
