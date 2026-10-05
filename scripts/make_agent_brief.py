#!/usr/bin/env python3
"""
Write the connection block the Claude project needs, with the real values in.

The Chrome session knows only what its saved process doc tells it. The app
cannot publish to the project, cannot register itself, and cannot be
discovered — so an API URL and an agent key that exist only in Render are
invisible to the session, and "I cannot find the approved invoices" is the
correct and unhelpful result.

This writes scripts/claude-project-brief.md: the connection block followed by
the whole of docs/AGENT_API.md, ready to paste into the project as one
document.

    python scripts/make_agent_brief.py https://your-service.onrender.com

Gitignored, because it contains the agent key.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

HEADER = """# Connecting to the Ferrocrete Invoice Processor

The review app is the source of truth for every value you type into
BuilderTrend. Read it first; do not re-derive anything from the PDF that the
app already gives you.

## Where it is

    API base URL:  {base}
    Auth header:   X-Agent-Key: {key}

Send that header on every call below. There is no login and no cookie.

## Start here, every session

    GET {base}/invoices/upload-queue

That returns the approved invoices and, for each one, the exact BuilderTrend
field values: the base cost code for the Title, the 4-digit Bill #, the
BuilderTrend vendor name, the dates, one Costs row per cost code, and a
signed URL for the PDF.

**The cost code is already decided.** A project engineer chose it and an
approver signed it off. It arrives as a value to type, never as a question to
answer. If one looks wrong, say so and flag the invoice — do not correct it
in the form, and do not pick a different code.

If that call returns an empty `queue`, there is genuinely nothing approved.
Say so and stop; do not go looking in Drive.

---

"""


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(
            "Usage: python scripts/make_agent_brief.py https://<api-host>\n"
            "The Render URL of the backend, not the Vercel URL."
        )
    base = sys.argv[1].rstrip("/")

    key = None
    credentials = HERE / "google-credentials.env"
    if credentials.exists():
        for line in credentials.read_text().splitlines():
            if line.startswith("AGENT_API_KEY="):
                key = line.split("=", 1)[1].strip()
    if not key:
        import os

        key = os.environ.get("AGENT_API_KEY")
    if not key:
        sys.exit(
            "No AGENT_API_KEY found in scripts/google-credentials.env or the "
            "environment. It must match the value set on Render."
        )

    contract = (REPO / "docs" / "AGENT_API.md").read_text()
    # The brief carries its own connection section, so drop the placeholder
    # auth section from the contract rather than contradicting it.
    marker = "\n## 1. Read the queue"
    body = contract[contract.index(marker):] if marker in contract else contract

    out = HERE / "claude-project-brief.md"
    out.write_text(HEADER.format(base=base, key=key) + body)
    out.chmod(0o600)

    print(f"\nWritten to {out}")
    print(f"  API base URL: {base}")
    print(f"  Agent key:    {key[:6]}…{key[-4:]} ({len(key)} chars)")
    print(
        "\nPaste the whole file into the 'Invoice Upload on Builder Trend'\n"
        "Claude project, alongside the BuilderTrend element ids and click\n"
        "snippets that already live there.\n"
        "\nIt contains the agent key, so it is gitignored. Anyone with access\n"
        "to that project can read the key; it is scoped to four endpoints\n"
        "(read the queue, read a PDF URL, report a save, flag) and can never\n"
        "approve anything."
    )


if __name__ == "__main__":
    main()
