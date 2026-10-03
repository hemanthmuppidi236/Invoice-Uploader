# Phase 1 — intake, AI suggestion, flagged triage

Phase 1 is the pipeline from a PDF landing in a Drive folder to an invoice
sitting in `suggested` with a cost code and a rationale, plus the triage queue
for everything that would not route on its own.

The Phase 1 gate from the build spec: **drop the CalPortland invoice in the
folder, see it appear with `3002` suggested and the `6011000` shear-wall code
as the alternative.** Everything the app needs for that is built. The
remaining requirement is Drive credentials and the invoice itself.

---

## 1. What the pipeline does

```
Drive folder
    │  every 15 min (Render cron → POST /jobs/poll-drive)
    ▼
invoices row, status `ingested`      ← claimed BEFORE the download
    │  PDF → Supabase Storage
    ▼
Claude reads the PDF                 ← §8 authority order
    │
    ├── vendor + project matched, no duplicate  →  `suggested`
    └── anything else                           →  `flagged` with a reason
```

Three properties are structural rather than incidental:

**Idempotent.** The `invoices` row is inserted *before* the download, so the
unique index on `(source_file_id, source_page)` is claimed before any slow
work. Two overlapping cron firings cannot both ingest the same file. This
matters because intake does not move the Drive original — that happens after a
verified BuilderTrend save in Phase 3 — so the same folder is re-read every
fifteen minutes indefinitely.

**Fails loudly.** Every failure path lands the invoice in `flagged` with the
error text attached. A download 403, an unreadable scan, a model timeout, an
invented cost code: all visible in `/flagged`, none silently skipped. A
skipped invoice is an unpaid vendor, and nobody finds that out from an empty
list.

**Never touches the Drive original.** There is no delete call anywhere in
`app/core/drive.py`. The only write operations are `upload_copy` and `move`,
and `move` reassigns parents rather than copying and deleting.

## 2. How the cost code gets chosen

This is the decision the whole app exists for. A mix number like `4018045`
legitimately serves columns, basement walls, retaining walls, and misc walls —
four different cost codes. §8 gives an authority order, and the extractor's
job is to hand the model what each level needs:

| Level | Evidence | Where it comes from |
|---|---|---|
| 1 | An explicit code written on the invoice | Read off the PDF. Wins outright, confidence ≥ 0.9. |
| 2 | The mix number on the line items | The project's confirmed `mix_designs`, with unmapped rows marked as unusable |
| 3 | The description text | `cost_codes.element_keywords` |
| 4 | The vendor's default code | `vendors.default_cost_code_id` — White Cap is always `3015` |

Plus three tiebreakers for when one mix serves several elements:

- **Phase inference.** The last 30 days of approved invoices on the same
  project, with the inferred current phase and the likely next ones stated
  explicitly rather than left for the model to derive from dates.
- **Quantity.** Under roughly 15 CY leans toward columns and walls; large
  pours toward slabs, decks, and footings.
- **Pump line size.** A mix batched for a 3" line is going somewhere a 4" line
  cannot reach.

When the comment and the mix disagree — the §8 worked example, and the gate
scenario — the comment stays as the suggestion at medium confidence and the
mix-derived code goes first in `alternatives` with a note. The reviewer sees
both readings; neither is silently discarded.

**A code the model invents is dropped, not created.** §12 forbids the app from
creating a cost code, and that includes alternatives: a one-click chip on the
review screen can never point at something that does not exist.

## 3. Flag reasons and what clears them

`/flagged` groups by reason, because the reasons need different fixes — every
"unknown vendor" is one missing mapping from resolving, while every "possible
duplicate" needs a judgement call.

| Reason | What it means | How it clears |
|---|---|---|
| `unknown_vendor` | The letterhead name is not in the vendor list | Add the mapping in `/admin`, then re-run the AI |
| `unknown_project` | The job could not be matched, or matched too weakly | Set the project inline, then re-run the AI |
| `new_project` | Matched a current job, which is out of scope (§4.1) | Nothing. It stays here by design. |
| `project_not_onboarded` | Real project, no PE or no mix design | Finish onboarding, then re-run |
| `possible_duplicate` | Same vendor + number, or same vendor + amount + date | Mark not a duplicate, or void |
| `unreadable` | A field would not read off the PDF | Fill it in by hand, or re-scan |
| `yard` | A White Cap yard invoice (SOP §5) | Void. These are never entered. |
| `ai_error` | The extraction call failed | Re-run. Twice failing usually means the PDF. |
| `stop_and_ask` | Something needed a person | Read the detail |

A weak project match is treated as **no match**. A confident-looking guess on
the wrong job is the expensive failure — a bill on the wrong job is worse than
a bill that waits — so anything under the confidence floor flags instead of
routing.

## 4. Setting up Drive

The poll needs unattended Google access. Two supported ways, covered in full
in **[docs/SETUP.md §4](SETUP.md)**:

- **A service account** with the shared drive folders shared to it. Preferred
  where your Google org allows creating one.
- **OAuth user credentials** with a refresh token, minted by
  `scripts/get_google_refresh_token.py --scopes drive`. The fallback when the
  org enforces `iam.disableServiceAccountCreation`, and the same mechanism
  Gmail sending uses.

Either way you also need the three folder ids, taken from the part of each
folder's URL after `/folders/`:

```bash
DRIVE_FOLDER_INVOICE_UPLOADS=1AbC...
DRIVE_FOLDER_WHITE_CAP=1DeF...          # optional
DRIVE_FOLDER_BT_INVOICES=1GhI...        # Phase 3 filing only
```

`GET /health` reports `"drive": true` and names the mode in `"drive_auth"`
once it is wired up. The failure worth knowing about: whichever identity you
use, if it cannot see the folders the Drive API returns an **empty list rather
than an error**, which is indistinguishable from "no new invoices".

### Render cron

```
Schedule:  */15 * * * *
Command:   curl -fsS -X POST "$API_URL/jobs/poll-drive" \
             -H "X-Agent-Key: $AGENT_API_KEY"
```

The endpoint is never open — it makes the app download arbitrary Drive files
on demand, so it requires the shared key. `limit` caps how many new invoices
one run ingests (default 50, max 200), because each one costs a Claude call
and a first run over a full backlog should be bounded.

The backlog is worked **oldest first**: with a cap in place, the order decides
who waits, and the oldest invoice is the most overdue vendor.

## 5. Running the gate

Two ways to get an invoice in. **`POST /invoices/upload`** (the **Upload a
PDF** button on `/invoices`, accounting only) hands the app a PDF directly —
for an emailed attachment, a re-scan of something that flagged as unreadable,
or a first invoice on a deployment with no Drive credentials yet. The Drive
poll is still the normal path and the one this gate is about.

Everything after the PDF reaches Storage is identical either way: same
extraction, same flag reasons, same states. The only difference is where
idempotency comes from. The poll keys on the Drive file id; an upload has no
file id, so it keys on a SHA-256 of the bytes — re-sending the same invoice
under a different filename is recognised, because for an upload the content
is the only identity there is.

1. Confirm `GET /health` shows `"claude": true` and `"drive": true`.
2. Onboard A Street Flats with its CMD-01 submittal (Phase 0), so mix
   `6011000` exists and maps to `3002 - Concrete Walls`.
3. Put the CalPortland invoice in `Invoice Uploads/` and either wait for the
   cron or press **Run intake now** on `/invoices`.
4. The invoice appears in `suggested`. Open it:
   - The suggested code is `3002 - Concrete Columns`, from the handwritten
     `Cost code:3002 column` comment.
   - Confidence is medium, not high, because the comment conflicts with the
     mix.
   - The alternatives list leads with `3002 - Concrete Walls`, noting that mix
     `6011000` is the 6000 psi shear-wall mix.
   - The rationale card says which authority level was used.

If the mix design has not been onboarded, the invoice flags with
`project_not_onboarded` instead — which is the correct behavior, and worth
seeing once.

## 6. Decisions taken in Phase 1

- **A retry cannot discard a human's judgment.** `retry-suggestion` refuses
  once `reviewed_at` is stamped. The line is not the status but whether
  someone has certified the values: a retry rewrites the line items and the
  cost split wholesale, and silently replacing a reviewer's work with a fresh
  guess would erase a decision without telling anyone.
- **A PATCH corrects what the invoice says, never where it is.**
  `InvoiceUpdate` has no `status` field and no workflow timestamp, so a
  smuggled one is a 422 naming the field. It is also refused outright once the
  invoice is past review.
- **Derived fields follow their source.** `bill_no` and `due_date` are
  computed from the invoice number and date, not accepted from the client, and
  clearing the invoice date clears the due date with it.
- **Cost rows are forced to sum to the total.** The model is told to make them
  sum and mostly does; when it does not, intake reallocates proportionally
  rather than storing a split that cannot be approved. A reviewer facing a
  silent one-cent gap has no way to tell a rounding artifact from a misread
  line.
- **Combined White Cap files are flagged, not parsed.** The page splitter is
  Phase 4. Reading a twelve-page multi-job bundle as one invoice would produce
  a confident wrong total, which is worse than a flag.
- **Yard detection matches known yard names, not the word "yard".** Entering a
  yard invoice is bad and skipping a real job invoice is worse, so a job
  called "Courtyard Apartments" does not trip it.
- **A voided invoice is never a duplicate match.** Voiding is how a real
  duplicate gets resolved, so matching against voided records would make the
  flag permanent.

## 7. Known gaps to pick up later

- **No structured JSON logging yet.** §12 wants the invoice id as a
  correlation id. Right now intake logs the invoice id in its messages and
  every 500 carries an `error_id`, but the format is plain text.
- **No Sentry.** §12 asks for it. The `error_id` in every 500 response is the
  stopgap.
- **`_lookups()` loads every user, project, vendor, and cost code per
  request.** Fine at Ferrocrete's scale — a few hundred rows — and it keeps
  the joins predictable, but it is the first thing to cache if the invoice
  list ever feels slow.
- **The duplicate check does not fuzzy-match amounts.** A re-scan that reads
  `2294.25` as `2294.26` slips through both tests. §13 lists duplicate
  detection hardening as Phase 4 work.
- **`poll-drive` is synchronous.** A run over 50 new invoices makes 50 Claude
  calls inside one HTTP request. The cap keeps it bounded, but a long run will
  hold the connection; a background queue is the eventual answer.
