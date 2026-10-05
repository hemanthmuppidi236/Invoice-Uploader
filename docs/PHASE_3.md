# Phase 3 — the BuilderTrend upload and the Drive filing

Phase 3 is the last leg: an approved invoice becomes a bill in BuilderTrend,
and its PDF gets filed back to Drive. It is also the only part of the app a
Claude in Chrome session touches.

The Phase 3 gate from the build spec: **Linda runs one real batch.**
Everything the app needs for that is built. What is left is credentials (Drive
and the agent key) and a session.

---

## 1. Who does what

```
  app                                  Chrome session
  ───                                  ──────────────
  GET /invoices/upload-queue   ───────▶ reads the field values
                                        drives the BuilderTrend form
                                        (SOP §4 steps 3–6)
  POST /invoices/{id}/mark-uploaded ◀── reports the saved bill id
      │
      ├─ stamps uploaded_at, bt_bill_id
      └─ files the PDF to Drive, stamps filed_at

  POST /invoices/{id}/flag          ◀── any stop-and-ask, then next invoice
```

Three boundaries are structural:

**The app never opens the browser.** §7.6: "Linda always starts the session."
There is no button for it on `/uploads` and there is no endpoint that would
trigger one. A session exists because a person decided to start one.

**The session can raise a hand, never lower one.** The agent key is scoped to
four calls: read the queue, read a PDF URL, report a save, and flag. It is
rejected on `approve` and `mark-reviewed` with an explanation rather than a
bare 403, because §12 requires a human stamp at both gates and a 403 with no
reason invites someone to go looking for a way around it.

**The backend files, not Chrome.** §14 left this open — "prefer backend since
it is deterministic, decide in Phase 3" — and the backend won on two counts.
The naming convention differs per vendor folder and has to be inferred from
what is already in it, which is parsing rather than browsing. And SOP §9
records five invoices filed with truncated names because a `$` went through a
shell unescaped; through the Drive API a filename is data, not a shell word,
so that class of bug cannot recur.

## 2. The upload queue

`GET /invoices/upload-queue` returns the BuilderTrend **field values**, not
the app's own record. Every value the session would otherwise derive is
derived server-side, where it is tested:

| Field | Where the value comes from |
|---|---|
| Bill Title | the base code of the largest cost-split share |
| Bill # | last 4 digits of the invoice number, suffixes ignored (`278461-1` → `8461`) |
| Pay to | `vendors.bt_name`, never the letterhead name |
| Invoice date | as printed |
| Due date | end of the month **after** the invoice date; vendor terms ignored |
| Costs rows | one per `invoice_costs` row, Title blank, Qty 1 |
| Bill URL | `buildertrend.net/app/Bills/Bill/0/{bt_job_id}` |
| PDF | a signed Storage URL, good for four hours |

Each item carries two lists, and the difference matters:

- **`warnings`** — proceed, but read this first. A missing job id, an invoice
  old enough to need a duplicate check, a credit memo, recorded project
  quirks.
- **`blockers`** — do not enter this one. Flag it and move on.

They are separate arrays rather than one severity field so a blocker cannot
be read as advisory. Items with blockers come back in a separate `blocked`
list, so the `queue` array is safe to iterate without checking.

### The multi-base-code case

BuilderTrend's Bill Title takes **one** base code, but an invoice can
legitimately split across two — 3002 for walls and 3008 for a slab on the
same ready-mix bill. The form cannot express that. The largest share wins,
every sub-code still goes on the Costs grid, and a warning names both codes
and which one was chosen. Ties break on the lower code so the answer does not
depend on row order.

Picking the first row silently was the alternative, and it would put a
plausible wrong code on a real bill that nobody would ever notice.

## 3. Recording the save

`POST /invoices/{id}/mark-uploaded` takes one field: `bt_bill_id`.

This is the endpoint §12 names — "reject if `approved_at` is null" — and the
check is a state-machine precondition rather than a line in the handler, so a
future caller cannot skip it. Both stamps are required, not either: an
approval without a review means the cost code was never checked by the person
who did the work, which is the exact failure the app exists to remove.

The bill id is also the whole idempotency story. SOP §8.4: *"Never click Save
twice without checking whether the first one fired — that's how duplicate
bills get created."*

| Call | Result |
|---|---|
| First report | `uploaded`, bill id stored, filing runs |
| Same bill id again | quiet success, `already_recorded: true`, nothing re-run |
| **Different** bill id on the same invoice | 409 — two bills exist, go delete one |
| Same bill id on a **different** invoice | 409 — the save landed on the wrong record |

A replay has to be cheap, because a session that loses its connection
mid-batch will retry and an operator who sees a scary error there starts
guessing. A second *bill*, though, needs a person. The unique index on
`bt_bill_id` is the real guarantee; the two checks are the readable version of
it, and a race between them is translated back into the same 409 rather than
surfacing as a 500.

## 4. Filing (§7.7)

Filing runs automatically inside `mark-uploaded`, and separately via
`POST /invoices/{id}/mark-filed` for a retry.

1. Resolve `BT Invoices/[Project]/[Vendor]/`. **A missing folder stops and
   asks** — §7.7 and §12 both forbid creating one, and a folder the app
   invents is a folder nobody is looking in.
2. Read the folder and infer its naming convention. Formats vary per folder
   (`26 09-23 $447.46.pdf`, `09-14-26 $2,600.00.pdf`), so the majority format
   in that folder wins and the default `MM-DD-YY $amount.pdf` is used only
   when nothing there matches. A folder that gave no clear answer says so in
   `filing_warnings`.
3. Upload the copy. A same-name collision is suffixed `(2)` and reported
   rather than overwritten.
4. Move the original to `Uploaded/`. **Never delete** — the move reassigns
   parents, so the Drive file id survives and re-polling still recognises the
   file as ingested.

Two properties are deliberate:

**Failing to file never rolls back the upload.** The bill is already in
BuilderTrend. Refusing the upload stamp because Drive said no would make a
successful save look like a failed one, and the session would try it again.
So the invoice stays in `uploaded` with `filing_error` set, which is exactly
what `/uploads` lists for a retry.

**A failed archive is a warning, not a failure.** The copy is the part that
matters, and re-running the whole thing would put a second copy in the vendor
folder — worse than an original left in place.

### Why a filing failure does not go to `/flagged`

§7.7 says to "flag" a missing folder. It is surfaced on `/uploads` instead,
because `/flagged` is the pre-BuilderTrend triage queue and its actions —
void, re-run the AI, reassign — all contradict a bill that already exists in
BuilderTrend. `POST /flag` refuses an `uploaded` or `filed` invoice for the
same reason. The invoice is still loud: it sits in an amber section on
`/uploads` with the reason and a retry button, and the invoice's own banner
says which half is outstanding.

## 5. Telling the Chrome session where the app is

The session knows only what its saved process doc tells it. The app cannot
publish to the Claude project, cannot register itself, and cannot be
discovered — so an API URL and an agent key that exist only in Render are
invisible to it, and "I cannot find the approved invoices" is the correct and
completely unhelpful result.

Two scripts close that gap:

```bash
# Does the app side work at all? Separates "the session was never told"
# from "the key is wrong" from "there is nothing approved" — three causes
# that look identical from inside a session.
python scripts/check_agent_access.py https://your-service.onrender.com

# The text to paste into the Claude project: connection block with the real
# URL and key, followed by the whole of docs/AGENT_API.md.
python scripts/make_agent_brief.py https://your-service.onrender.com
```

The brief is gitignored because it carries the agent key. Anyone with access
to that Claude project can read it — which is bounded by design: the key
reaches four endpoints (read the queue, read a PDF URL, report a save, flag)
and can never approve anything. The guardrails live in the API, not in the
doc, precisely because the doc is editable by anyone with project access.

## 6. Setting up

### Drive

Filing needs the `BT Invoices` folder id on top of what Phase 1 already
needed:

```bash
DRIVE_FOLDER_BT_INVOICES=1GhI...     # the part of the folder URL after /folders/
```

`GET /health` reports `"filing": true` once Drive credentials **and** that id
are both present. It is reported separately from `"drive"` on purpose: intake
needs only the intake folder, so a deploy with credentials and no
`BT_INVOICES` looks healthy right up to the first successful upload.

The Drive identity needs write access to `BT Invoices` and to the intake
folders. Read-only credentials pass the health check and fail at the first
copy.

### The agent key

```bash
AGENT_API_KEY=$(openssl rand -hex 32)
```

One shared secret, sent as `X-Agent-Key`, used by both the Chrome session and
the Render cron jobs. Compared with `secrets.compare_digest`, and an unset key
**denies** rather than allowing — a deploy that forgets it locks the agent out
instead of opening the door.

## 7. Running the gate

1. Confirm `GET /health` shows `"filing": true` and `"agent_auth": true`.
2. Get an invoice all the way to `approved` (Phase 2).
3. Open `/uploads` as Linda. Check the card against the PDF — the card *is*
   the entry, not a summary of it.
4. Copy the prompt and start a session in the "Invoice Upload on Builder
   Trend" project with Chrome signed in to BuilderTrend.
5. The session works the queue, reports each save, and flags anything it had
   to stop on.
6. Back on `/uploads`: the bill appears under recent sessions with its
   BuilderTrend bill id and the filed path.

Worth doing deliberately once:

- **Report the same bill id twice.** Quiet success — that is a replay.
- **Report a different bill id for an invoice already uploaded.** A 409 that
  tells you two bills exist.
- **Rename the vendor's Drive folder, then upload.** The bill saves, the
  filing stops and asks, and `/uploads` shows the retry. Rename it back and
  press retry.

## 8. Decisions taken in Phase 3

- **The backend files, not Chrome** (§14's open question). Deterministic
  parsing beats browsing, and it removes the SOP §9 shell-escaping failure
  mode entirely.
- **The upload queue is its own endpoint**, not `GET /invoices?status=approved`
  as §7.6 sketches. The list payload is shaped for a table; the session needs
  form values. Same data, and computing the derivations once — server-side,
  tested — is what stops a wrong Bill # reaching a real ledger.
- **Blockers and warnings are separate arrays.** One severity field invites a
  blocker being treated as advisory.
- **A cross-base-code split warns rather than blocks.** The invoice is
  already approved; refusing to enter it would strand real money over a form
  limitation. The Title is chosen by share and the choice is stated.
- **A zero-amount invoice warns rather than blocks.** Someone reviewed and
  approved it. Odd is not impossible.
- **An inactive cost code warns; a deleted one blocks.** "Inactive here" and
  "not selectable in BuilderTrend" are different facts; a code that no longer
  exists is not a fact at all.
- **Flag is the only write the agent gets.** §7.6 step 5 needs it, and it can
  only ever raise a hand.
- **Transitions match the status in the UPDATE, not just before it.** Two
  callers who both read `approved` must not both write. This closed a real
  window for approve as well as for mark-uploaded.
- **`move` is idempotent.** A combined White Cap PDF is one Drive file behind
  several invoice rows, so the second page's filing asks to archive a file the
  first page already archived.

## 9. Known gaps to pick up later

- **The queue generates one signed URL per invoice, synchronously.** A daily
  batch is fine; a first run over a 100-invoice backlog makes 100 Storage
  calls in one request. A failure on any single object blocks only that
  invoice, not the queue.
- **White Cap originals archive to `White Cap/Uploaded/`, not
  `White Cap/Uploaded/[Job Name]/`.** SOP §5 wants the per-job subfolder; it
  arrives with the Phase 4 page splitter that creates those jobs in the first
  place.
- **Nothing reconciles against BuilderTrend.** The app trusts the session's
  report that a save was verified. A bill deleted in BuilderTrend afterwards
  leaves this app saying `uploaded` forever, and only a person notices.
- **No batch summary record.** "Recent sessions" is derived from
  `uploaded_at`, so two sessions in one day read as one. SOP §10's
  end-of-batch checklist lives in the chat, not here.
- **`filed_file_id` is stored but never used.** It is there so a filed copy
  can be verified later without a name search; nothing verifies yet.
