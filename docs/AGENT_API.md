# The agent API — what the Chrome session talks to

This is the contract between the Ferrocrete Invoice Processor and the Claude
in Chrome session that enters bills in BuilderTrend. It is meant to be pasted
into (or referenced from) the saved process doc in the **Invoice Upload on
Builder Trend** project, alongside the BuilderTrend element ids and click
snippets that live there.

It covers what the app gives you and what it expects back. It does **not**
cover how to drive BuilderTrend — that is the SOP and the project doc.

---

## Auth

Every call below takes one header:

```
X-Agent-Key: <AGENT_API_KEY>
```

No bearer token, no user session. The key is scoped to exactly four calls.
**`approve` and `mark-reviewed` will refuse it**, and say why: no invoice
reaches BuilderTrend without a human review stamp and a human approval stamp.
If either is missing, the app is telling you the invoice is not ready and the
answer is never to work around it.

Base URL is the backend, not the frontend: `https://<api-host>`.

---

## 1. Read the queue

```
GET /invoices/upload-queue
```

Optional `?limit=` (default 100, max 500). Oldest-approved first.

```jsonc
{
  "generated_at": "2026-09-30T16:02:11Z",
  "count": 3,
  "filing_by_backend": true,
  "notes": ["..."],
  "queue": [
    {
      "invoice_id": "8f2c…",

      // Where the bill goes. Open by URL — job context drifts (SOP §8.5) —
      // then confirm the Job field on the form before typing anything.
      "bt_job_id": "778899",
      "bill_url": "https://buildertrend.net/app/Bills/Bill/0/778899",
      "project_no": "25-20",
      "project_name": "A Street Flats",

      // The form, in SOP §4 order. Type these verbatim.
      "bill_title": "3002",          // Title, next to Bill #: the BASE code
      "bill_no": "8461",             // Bill #: last 4 digits, suffix ignored
      "pay_to": "Catalina Pacific",  // NOT the letterhead name
      "invoice_no": "278461-1",
      "invoice_date": "2026-09-14",
      "due_date": "2026-10-31",      // already end-of-next-month
      "amount": "2600.00",           // Unit cost, Qty = 1
      "is_credit": false,

      // One Costs row each. Row Title stays BLANK.
      "costs": [
        { "cost_code": "3002 - Concrete Walls", "base_code": "3002",
          "name": "Concrete Walls", "amount": "2600.00", "note": null }
      ],

      // Custom fields → Invoice. NOT Attachments.
      "pdf_url": "https://…signed…",
      "pdf_expires_in": 14400,

      "quirks": {},
      "project_notes": null,
      "vendor_notes": null,
      "age_days": 16,

      "warnings": [],   // proceed, but read these
      "blockers": []    // never populated in `queue`
    }
  ],
  "blocked": [ /* same shape, with blockers set */ ],
  "awaiting_filing": [ /* uploaded but not filed */ ],
  "recently_uploaded": [ /* context for your closing summary */ ]
}
```

### How to treat the two lists

**`queue`** — work these. Read every `warnings` entry before starting the
invoice; they are the per-record version of the SOP's stop-and-ask rules
(an old invoice needing a duplicate check, a missing job id, a credit memo,
recorded project quirks).

**`blocked`** — do not enter these. Each one carries `blockers` saying what
BuilderTrend cannot be given a value for. They are approved, so the numbers
are trusted; what is missing is reference data only a person can supply.
Mention them in your summary and move on. Do not improvise a value, and do
not fall back to a "close enough" vendor or cost code.

### Do not re-derive anything

The Title, the Bill #, the due date and the BuilderTrend vendor name are all
computed server-side and tested there. If one looks wrong, say so — do not
correct it in the form. A field that disagrees with the API is a bug worth
knowing about; a quietly corrected field is a bill nobody can reconcile.

### If a PDF URL has expired

A batch can outrun the four-hour signature. Ask for a fresh one:

```
GET /invoices/{invoice_id}/pdf-url
```

---

## 2. Report a save

Only after the SOP §4.6 verification: status **Open** (not Draft), dates
right, vendor right, PDF attached.

```
POST /invoices/{invoice_id}/mark-uploaded
Content-Type: application/json

{ "bt_bill_id": "1234567", "note": "optional" }
```

`bt_bill_id` is required and must be the real BuilderTrend bill id — it is the
only thing that can tell a repeated call apart from a second bill.

Response:

```jsonc
{
  "invoice": { … },
  "already_recorded": false,
  "filing": {
    "filed": true,
    "filed_path": "BT Invoices/A Street Flats/CalPortland/09-14-26 $2,600.00.pdf",
    "original_archived": true,
    "error": null,
    "needs_folder": false,
    "warnings": []
  }
}
```

Filing runs inside this call, so **there is nothing for you to do on Drive.**

| You get | What it means | What to do |
|---|---|---|
| `already_recorded: true` | You already reported this bill. A replay. | Nothing. Move on; do not count it twice. |
| `filing.filed: true` | Copy filed, original archived. | Nothing. |
| `filing.error` set | The bill is fine; the Drive half is not. | Note it in your summary. Do not save the bill again. |
| `409` | Read the message. Usually two bills now exist for one invoice. | **Stop.** Tell Linda. Do not retry. |
| `422` | A human stamp is missing. | Do not save this bill. Hand it back. |

### The one thing that must not happen

SOP §8.4: *"Never click Save twice without checking whether the first one
fired."* If you are unsure whether a save landed, **check BuilderTrend, not
this API.** Reporting the same bill id twice is harmless. Saving twice
creates a second bill, and this API will then refuse the second report with a
409 — which is the app catching a mistake that already happened, not
preventing it.

---

## 3. Stop and ask

Any SOP §7 condition — vendor not found, no clear cost code, the job could be
more than one record, a Drive folder missing, a filename collision at a
different size, BuilderTrend rejecting the bill number as a duplicate, a Bill
dialog already open that you did not open:

```
POST /invoices/{invoice_id}/flag
{ "code": "stop_and_ask", "detail": "what you saw, in one or two sentences" }
```

Then **continue with the next invoice.** Leave the record where it is; do not
save a partial bill, and do not delete a draft you did not create.

Valid codes: `stop_and_ask` (the usual one), `unknown_vendor`,
`unknown_project`, `possible_duplicate`, `unreadable`, `other`.

`detail` is read by a person tomorrow morning. "Vendor search for 'Cefali'
returned two entries, one marked DO NOT SELECT" is useful; "could not find
vendor" is not.

Flagging an invoice that is already `uploaded` or `filed` is refused with a
409 — a bill that exists in BuilderTrend must not go back into a triage queue
whose actions would contradict it. If something is wrong with a bill you
already saved, say so in your summary instead.

---

## 4. Filing it yourself (not the default)

The backend files, so you should not need this. It exists because §14 allows
Chrome to do the step if Drive permissions make the backend route awkward, and
because a filing done by hand still needs recording.

```
POST /invoices/{invoice_id}/mark-filed
{ "filed_path": "BT Invoices/A Street Flats/CalPortland/09-14-26 $2,600.00.pdf",
  "original_archived": true }
```

Send `{}` instead and the backend files it now — that is the retry path.

Check `filing_by_backend` in the queue response before assuming either way.

If you ever do file through a terminal rather than the Drive API: **escape the
`$`** (SOP §9). `$2,294.25` silently became `,294.25` on five invoices once.
This is the whole reason filing moved into the backend.

---

## 5. What the app will never let you do

Not restrictions to work around — they are the reasons the app exists.

- **Approve or mark reviewed.** Both require a person. The agent key is
  refused on both.
- **Create a vendor, a cost code, or a Drive folder.** Every one of those is
  a stop-and-ask.
- **Delete a Drive file.** There is no delete call anywhere in the app.
  Originals are moved to `Uploaded/`.
- **Enter an invoice with an unbalanced cost split.** It cannot reach
  `approved`, so it will never be in your queue.
- **Enter a yard invoice.** Intake flags those; they never reach `approved`.
