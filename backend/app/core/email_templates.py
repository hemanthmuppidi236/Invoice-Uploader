"""
The two daily emails (prompt §9), in the app's design language.

Everything is inline-styled. Email clients strip `<style>` blocks and most
ignore external stylesheets, so the CHAD palette is repeated as literal hex
per element rather than referenced as a token. The palette here is the
light-mode half of globals.css — cream ground, gold accent, Ferrocrete red —
so the mail reads as the same product as the screen it links to.

Pure string building: these functions take rows and return HTML. Deciding who
gets mail lives in `email_jobs.py`, which keeps the copy testable without a
database.
"""

from decimal import Decimal
from typing import Optional

from .config import settings

# ─── Palette (light mode from globals.css) ────────────────────────────

GROUND = "#f3ecdc"
CARD = "#fefaf2"
BORDER = "#d9c9a6"
TEXT = "#2a2820"
TEXT_MUTED = "#5a5650"
TEXT_FAINT = "#807a72"
ACCENT = "#b8852a"
ACCENT_TEXT = "#6e4f10"
ACCENT_DIM = "#f6efe0"
RED = "#d53b34"
GREEN = "#3a7a56"
AMBER = "#9a7020"

SERIF = "'EB Garamond', Georgia, 'Times New Roman', serif"
MONO = "'IBM Plex Mono', Menlo, Consolas, monospace"


def _esc(value) -> str:
    if value is None:
        return ""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _money(value) -> str:
    if value is None:
        return "—"
    try:
        amount = Decimal(str(value))
    except Exception:
        return _esc(value)
    if amount < 0:
        return f"(${abs(amount):,.2f})"
    return f"${amount:,.2f}"


def _app_url(path: str = "") -> str:
    return f"{settings.app_url.rstrip('/')}{path}"


def confidence_color(confidence: Optional[float]) -> str:
    """§9.1 asks for the suggested code "with confidence color".

    Same thresholds as the app's pills, so gold in the mail means the same
    thing as gold on the screen.
    """
    if confidence is None:
        return TEXT_FAINT
    if confidence >= settings.confidence_high:
        return GREEN
    if confidence >= settings.confidence_low:
        return AMBER
    return RED


def confidence_text(confidence: Optional[float]) -> str:
    if confidence is None:
        return "no suggestion"
    return f"{round(confidence * 100)}%"


# ─── Shell ────────────────────────────────────────────────────────────


def _shell(*, title: str, subtitle: str, body: str, footer: str = "") -> str:
    """The outer table every message shares.

    Tables rather than divs, and a fixed 640px width: Outlook's renderer has
    no flexbox, and a percentage-width layout collapses there.
    """
    return f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width"></head>
<body style="margin:0;padding:0;background:{GROUND};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
       style="background:{GROUND};padding:28px 12px;">
  <tr><td align="center">
    <table role="presentation" width="640" cellpadding="0" cellspacing="0"
           style="max-width:640px;width:100%;background:{CARD};
                  border:1px solid {BORDER};border-radius:10px;
                  overflow:hidden;">
      <tr>
        <td style="padding:26px 30px 18px;border-bottom:1px solid {BORDER};">
          <div style="font-family:{MONO};font-size:10px;letter-spacing:1.4px;
                      text-transform:uppercase;color:{ACCENT_TEXT};
                      margin-bottom:8px;">
            Ferrocrete Builders, Inc.
          </div>
          <div style="font-family:{SERIF};font-size:25px;line-height:1.2;
                      color:#14140e;margin-bottom:6px;">
            {title}
          </div>
          <div style="font-family:{SERIF};font-size:15px;line-height:1.5;
                      color:{TEXT_MUTED};">
            {subtitle}
          </div>
        </td>
      </tr>
      <tr><td style="padding:22px 30px 26px;">{body}</td></tr>
      <tr>
        <td style="padding:16px 30px 22px;border-top:1px solid {BORDER};
                   background:{ACCENT_DIM};">
          <div style="font-family:{SERIF};font-size:12.5px;line-height:1.6;
                      color:{TEXT_FAINT};">
            {footer or "Sent by the Ferrocrete invoice processor."}
          </div>
        </td>
      </tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""


def _button(label: str, href: str) -> str:
    return f"""\
<table role="presentation" cellpadding="0" cellspacing="0" style="margin:18px 0 4px;">
  <tr><td style="background:{ACCENT};border-radius:24px;">
    <a href="{_esc(href)}"
       style="display:inline-block;padding:11px 22px;font-family:{SERIF};
              font-size:15px;color:#fffdf8;text-decoration:none;">
      {_esc(label)}
    </a>
  </td></tr>
</table>"""


def _th(label: str, align: str = "left") -> str:
    return (
        f'<th align="{align}" style="font-family:{MONO};font-size:9.5px;'
        f"letter-spacing:1px;text-transform:uppercase;color:{TEXT_FAINT};"
        f"font-weight:normal;padding:0 10px 8px;"
        f'border-bottom:1px solid {BORDER};">{_esc(label)}</th>'
    )


def _td(content: str, align: str = "left", color: str = TEXT) -> str:
    return (
        f'<td align="{align}" style="font-family:{SERIF};font-size:14px;'
        f"color:{color};padding:10px;border-bottom:1px solid {BORDER};"
        f'vertical-align:top;">{content}</td>'
    )


# ─── §9.1 Daily "in your court" ───────────────────────────────────────


def daily_court_subject(*, count: int, role: str) -> str:
    """§9.1 quotes these subject lines verbatim, so they are reproduced
    verbatim: "{n} invoices waiting for your review" / "... approval"."""
    noun = "invoice" if count == 1 else "invoices"
    what = "approval" if role == "approver" else "review"
    return f"{count} {noun} waiting for your {what}"


def daily_court_html(*, name: str, role: str, invoices: list[dict]) -> str:
    """One person's pending queue.

    `invoices` rows carry: id, vendor_name, project_name, project_no, amount,
    invoice_no, suggested_code, suggested_confidence, age_days.
    """
    is_approver = role == "approver"
    what = "approve" if is_approver else "review"
    queue_label = "Open your approval queue" if is_approver else "Open your review queue"

    rows = []
    for inv in invoices:
        link = _app_url(f"/invoices/{inv['id']}")
        code = inv.get("suggested_code")
        confidence = inv.get("suggested_confidence")
        age = inv.get("age_days")

        code_cell = (
            f'<span style="font-family:{MONO};font-size:12.5px;">{_esc(code)}</span>'
            f'<br><span style="font-family:{MONO};font-size:11px;'
            f'color:{confidence_color(confidence)};">'
            f"{_esc(confidence_text(confidence))}</span>"
            if code
            else f'<span style="color:{RED};">no suggestion</span>'
        )

        project_cell = _esc(inv.get("project_name") or "unmatched")
        if inv.get("project_no"):
            project_cell += (
                f'<br><span style="font-size:12px;color:{TEXT_FAINT};">'
                f"{_esc(inv['project_no'])}</span>"
            )

        age_cell = "—" if age is None else f"{age}d"
        age_color = RED if (age is not None and age >= 7) else TEXT_MUTED

        rows.append(
            "<tr>"
            + _td(
                f'<a href="{_esc(link)}" style="color:{ACCENT_TEXT};'
                f'text-decoration:none;">'
                f"{_esc(inv.get('vendor_name') or 'Unknown vendor')}</a>"
                + (
                    f'<br><span style="font-family:{MONO};font-size:11.5px;'
                    f'color:{TEXT_FAINT};">{_esc(inv["invoice_no"])}</span>'
                    if inv.get("invoice_no")
                    else ""
                )
            )
            + _td(project_cell)
            + _td(_money(inv.get("amount")), align="right")
            + _td(code_cell)
            + _td(age_cell, align="right", color=age_color)
            + "</tr>"
        )

    table = f"""\
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
  <tr>{_th('Vendor')}{_th('Project')}{_th('Amount', 'right')}
      {_th('Suggested code')}{_th('Waiting', 'right')}</tr>
  {''.join(rows)}
</table>"""

    count = len(invoices)
    noun = "invoice" if count == 1 else "invoices"

    body = f"""\
<div style="font-family:{SERIF};font-size:15px;line-height:1.6;color:{TEXT};
            margin-bottom:18px;">
  {_esc(name)}, {count} {noun} {'is' if count == 1 else 'are'} waiting for you
  to {what}.
  {'The reviewer has already checked the values and picked the cost code.'
   if is_approver
   else 'The cost code the AI suggested is preselected — change it if the job says otherwise.'}
</div>
{table}
{_button(queue_label, _app_url('/invoices'))}"""

    footer = (
        "Nothing is entered in BuilderTrend until a reviewer and an approver "
        "have both signed off. Sent once a day at 3:30 PM Pacific; you only "
        "get this when something is actually waiting."
    )

    return _shell(
        title=f"{count} {noun} in your court",
        subtitle=(
            "Awaiting your approval" if is_approver else "Assigned to you for review"
        ),
        body=body,
        footer=footer,
    )


# ─── §9.2 End-of-day summary ──────────────────────────────────────────


def eod_subject(*, approved_count: int) -> str:
    noun = "invoice" if approved_count == 1 else "invoices"
    return f"End of day: {approved_count} {noun} approved"


def eod_html(*, groups: list[dict], counts: dict) -> str:
    """§9.2: grouped by project, with a counts footer.

    `groups` is [{project_name, project_no, invoices: [...]}, ...]; each
    invoice carries vendor_name, invoice_no, amount, cost_codes (a list of
    code strings), reviewer_name, approver_name.
    `counts` carries approved_today, uploaded_today, still_pending, flagged.
    """
    sections = []
    for group in groups:
        rows = []
        for inv in group["invoices"]:
            codes = inv.get("cost_codes") or []
            code_cell = (
                "<br>".join(
                    f'<span style="font-family:{MONO};font-size:12.5px;">'
                    f"{_esc(c)}</span>"
                    for c in codes
                )
                if codes
                else f'<span style="color:{RED};">none</span>'
            )
            rows.append(
                "<tr>"
                + _td(
                    f'<a href="{_esc(_app_url("/invoices/" + inv["id"]))}" '
                    f'style="color:{ACCENT_TEXT};text-decoration:none;">'
                    f"{_esc(inv.get('vendor_name') or 'Unknown vendor')}</a>"
                )
                + _td(
                    f'<span style="font-family:{MONO};font-size:12.5px;">'
                    f"{_esc(inv.get('invoice_no') or '—')}</span>"
                )
                + _td(_money(inv.get("amount")), align="right")
                + _td(code_cell)
                + _td(_esc(inv.get("reviewer_name") or "—"))
                + _td(_esc(inv.get("approver_name") or "—"))
                + "</tr>"
            )

        group_total = sum(
            Decimal(str(i.get("amount") or 0)) for i in group["invoices"]
        )

        sections.append(
            f"""\
<div style="margin-bottom:26px;">
  <div style="font-family:{MONO};font-size:10px;letter-spacing:1.2px;
              text-transform:uppercase;color:{ACCENT_TEXT};
              padding-bottom:8px;">
    {_esc(group.get('project_name') or 'No project')}
    {f"· {_esc(group['project_no'])}" if group.get('project_no') else ''}
    · {_money(group_total)}
  </div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
    <tr>{_th('Vendor')}{_th('Invoice #')}{_th('Amount', 'right')}
        {_th('Cost code')}{_th('Reviewer')}{_th('Approver')}</tr>
    {''.join(rows)}
  </table>
</div>"""
        )

    total = sum(
        Decimal(str(i.get("amount") or 0))
        for g in groups
        for i in g["invoices"]
    )

    body = f"""\
<div style="font-family:{SERIF};font-size:15px;line-height:1.6;color:{TEXT};
            margin-bottom:22px;">
  {counts.get('approved_today', 0)} invoice{'' if counts.get('approved_today') == 1 else 's'}
  totalling <strong>{_money(total)}</strong> were approved today and are ready
  for the next BuilderTrend upload session.
</div>
{''.join(sections)}
{_counts_block(counts)}
{_button('Open the invoice queue', _app_url('/invoices'))}"""

    return _shell(
        title="End of day",
        subtitle="Invoices approved today, grouped by project",
        body=body,
        footer=(
            "Approved invoices wait for a Chrome upload session — the app never "
            "drives BuilderTrend itself. Sent at 5:30 PM Pacific, and only on "
            "days something was approved."
        ),
    )


def _counts_block(counts: dict) -> str:
    """§9.2 footer counts: approved today, uploaded today, still pending, flagged."""
    cells = [
        ("Approved today", counts.get("approved_today", 0), ACCENT_TEXT),
        ("Uploaded today", counts.get("uploaded_today", 0), TEXT_MUTED),
        ("Still pending", counts.get("still_pending", 0), TEXT_MUTED),
        ("Flagged", counts.get("flagged", 0), RED if counts.get("flagged") else TEXT_MUTED),
    ]
    tds = "".join(
        f"""\
<td width="25%" align="center" style="padding:12px 6px;">
  <div style="font-family:{MONO};font-size:9.5px;letter-spacing:1px;
              text-transform:uppercase;color:{TEXT_FAINT};margin-bottom:5px;">
    {_esc(label)}
  </div>
  <div style="font-family:{SERIF};font-size:24px;color:{color};">{value}</div>
</td>"""
        for label, value, color in cells
    )
    return f"""\
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
       style="margin-top:8px;background:{ACCENT_DIM};border:1px solid {BORDER};
              border-radius:8px;">
  <tr>{tds}</tr>
</table>"""
