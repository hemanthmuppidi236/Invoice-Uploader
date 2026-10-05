#!/usr/bin/env python3
"""
Prove the agent key can actually read the upload queue.

When a Chrome session reports that it cannot find the approved invoices,
there are only three possible causes, and they need completely different
fixes:

  1. The session was never told the API URL or the key. It is not looking.
  2. The key is wrong or unset on Render, so it is looking and getting 401.
  3. There genuinely is nothing approved.

A session saying "I can't find them" looks identical in all three cases.
This separates them in one call, from outside the session.

    python scripts/check_agent_access.py https://your-service.onrender.com

Reads AGENT_API_KEY from the environment, or from
scripts/google-credentials.env. Prints no secrets.
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_key() -> str:
    import os

    if os.environ.get("AGENT_API_KEY"):
        return os.environ["AGENT_API_KEY"]
    path = HERE / "google-credentials.env"
    if path.exists():
        for line in path.read_text().splitlines():
            if line.startswith("AGENT_API_KEY="):
                return line.split("=", 1)[1].strip()
    sys.exit(
        "No AGENT_API_KEY found.\n"
        "Set it in the environment, or keep scripts/google-credentials.env.\n"
        "It must be the same value as AGENT_API_KEY on Render."
    )


def get(url: str, key: str | None) -> tuple[int, object]:
    request = urllib.request.Request(url)
    if key:
        request.add_header("X-Agent-Key", key)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, body
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("Usage: python scripts/check_agent_access.py https://<api-host>")
    base = sys.argv[1].rstrip("/")
    key = load_key()

    print(f"\nAPI: {base}")
    print(f"Key: {key[:6]}…{key[-4:]} ({len(key)} chars)\n")

    status, health = get(f"{base}/health", None)
    if status != 200:
        sys.exit(
            f"/health returned {status or 'no response'}: {health}\n"
            "The API is not reachable, so nothing else here matters. Check "
            "the URL and that the Render service is live."
        )
    integrations = health.get("integrations", {})
    print("  /health                 OK")
    for name, value in integrations.items():
        mark = "OK  " if value else "off "
        print(f"    {mark}{name}: {value}")
    if not integrations.get("agent_auth"):
        sys.exit(
            "\nAGENT_API_KEY is not set on Render. Every agent call will 401, "
            "and an unset key denies rather than allows — which is the safe "
            "default, but it means the session cannot read anything."
        )

    # The wrong-key case, told apart from the no-key case. A wrong key is a
    # misconfigured session; no key is a session that was never told.
    status, _ = get(f"{base}/invoices/upload-queue", "deliberately-wrong-key")
    print(f"\n  wrong key rejected      {'OK' if status in (401, 403) else f'PROBLEM: {status}'}")

    status, queue = get(f"{base}/invoices/upload-queue", key)
    if status != 200:
        sys.exit(
            f"\n  upload-queue            FAILED {status}: {queue}\n"
            "\nThe key here does not match the one on Render. Compare both, "
            "character for character — a trailing space is invisible in both "
            "dashboards."
        )

    ready = queue.get("queue", [])
    blocked = queue.get("blocked", [])
    print("  upload-queue            OK\n")
    print("=" * 68)
    print(f"  {len(ready)} invoice(s) ready to upload")
    print(f"  {len(blocked)} blocked (missing something BuilderTrend needs)")
    print(f"  {len(queue.get('awaiting_filing', []))} uploaded but not yet filed")
    print("=" * 68)

    for item in ready:
        print(
            f"\n  {item.get('pay_to')} · {item.get('project_name')} · "
            f"${item.get('amount')}"
        )
        print(f"    Title {item.get('bill_title')} · Bill # {item.get('bill_no')}")
        for warning in item.get("warnings", []):
            print(f"    ! {warning}")
    for item in blocked:
        print(f"\n  BLOCKED: {item.get('pay_to')} · ${item.get('amount')}")
        for blocker in item.get("blockers", []):
            print(f"    x {blocker}")

    if not ready and not blocked:
        print(
            "\nThe API works and the key is right — there is simply nothing "
            "in `approved`.\nAn invoice reaches this queue only after a human "
            "approval stamp (§12).\nCheck /invoices?status=approved in the app."
        )
    elif ready:
        print(
            "\nThe app side is fine. If the Chrome session still cannot find "
            "these,\nit has not been told the API URL and key — see "
            "scripts/make_agent_brief.py."
        )


if __name__ == "__main__":
    main()
