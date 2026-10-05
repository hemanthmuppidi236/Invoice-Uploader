# The Chrome session playbook

Everything a Claude in Chrome session needs to turn approved invoices into
BuilderTrend bills: where the app is, what it hands over, how to drive the
form, and when to stop and ask.

This merges two sources that were separate and could drift apart:

- **`docs/AGENT_API.md`** — the app's contract.
- **The BuilderTrend Handover SOP** — the manual process this automates.

Where they disagree, **this document wins**, and the API wins over both: the
app enforces its own rules server-side regardless of what any document says.

> **Still missing:** the element ids and JavaScript click snippets from
> `claude/buildertrend-invoice-upload-process.md`. Paste that in alongside
> this. Without it a session has to find selectors by reading the page, which
> works but is slower and less reliable than named handles. Everything else —
> the quirks, the order of operations, the stop-and-ask rules — is here.

---

## 1. Connection

The connection block with the live API URL and agent key is generated
separately, because it contains a secret:

```bash
python scripts/make_agent_brief.py https://<your-api-host>
```

That writes `scripts/claude-project-brief.md`, which is this document with
the connection block prepended. Paste that file, not this one.

## 2. The order of operations

```
1. GET /invoices/upload-queue          ← read EVERYTHING first
2. For each item in `queue`:
     a. open the bill form on the right job
     b. fill it from the item's fields, verbatim
     c. attach the PDF
     d. save, then VERIFY the save
     e. POST /invoices/{id}/mark-uploaded  { "bt_bill_id": "…" }
3. Report `blocked` items without touching them
4. Summarise: saved, flagged, skipped
```

Read the whole queue before opening BuilderTrend. SOP §4.1: *"Read every PDF
first, before opening BuilderTrend."* The same reason applies to the queue —
knowing there are three invoices on one job changes how you work them.

## 3. The app decides; you type

Every value comes from the queue payload. **Do not re-derive anything.**

| BuilderTrend field | Queue field | Notes |
|---|---|---|
| Title (next to Bill #) | `bill_title` | The **base** code, e.g. `3002` |
| Bill # | `bill_no` | Last 4 digits; suffixes already stripped |
| Pay to | `pay_to` | BuilderTrend's name, not the letterhead |
| Invoice date | `invoice_date` | As printed on the invoice |
| Due date | `due_date` | Already end-of-month-after; vendor terms ignored |
| Costs row → Cost code | `costs[].cost_code` | One row per entry |
| Costs row → Title | — | **Leave blank** |
| Costs row → Unit cost | `costs[].amount` | Qty = 1. Negative for credits |
| Send to QuickBooks | — | **Checked** |
| Billable to Client | — | Unchecked |
| PDF | `pdf_url` | **Custom fields → Invoice**, not Attachments |

**The cost code is already decided.** A project engineer chose it and an
approver signed it off. It arrives as a value to type, never as a question to
answer. If one looks wrong, flag the invoice and say so — do not correct it
in the form, and do not pick a different code. A field that disagrees with
the API is a bug worth reporting; a quietly corrected field is a bill nobody
can reconcile.

The app has no endpoint that would let you change a cost code. That is
deliberate, not an oversight.

## 4. Driving the form (SOP §4)

1. **Open the bill on the correct job.** Use `bill_url` when the queue
   provides one. If it is null, the project has no BuilderTrend job id
   recorded: navigate to the job by name and **confirm the Job field on the
   form** before typing anything.
2. **Verify the Job field** regardless. SOP §8.5: job context drifts.
3. Fill the fields from the table above.
4. **Review, then Save to Job.**
5. **Verify it saved**: status **Open** (not Draft), dates correct, vendor
   correct, PDF attached.
6. Only then report it.

## 5. The quirks that cost time (SOP §8)

These are not edge cases. Budget for them on every bill.

1. **The two date fields fight each other.** Only one date commits per page
   load. Fill everything plus one date → **Save draft** → reload →
   **re-upload the PDF** (it does not survive the draft) → set the second
   date → **Save to Job**.
2. **Save clicks silently fail.** Click Save draft and Save to Job via
   JavaScript on the button element, never by screen coordinates.
3. **Vendor reverts to "Misc" at random.** Re-check it immediately before
   every save.
4. **Never click Save twice without checking whether the first fired.** This
   is how duplicate bills get created. See §6.
5. **Job context drifts.** Open new bills by URL and confirm the Job field.
6. **Dropdowns: do not use arrow keys + Enter.** It can land on "Create
   Sub/Vendor", and creating a vendor is forbidden (§12).
7. **Do not press Escape once dates are entered.** It can clear them.

## 6. Reporting a save, and the duplicate rule

After a *verified* save only:

```
POST /invoices/{invoice_id}/mark-uploaded
{ "bt_bill_id": "1234567" }
```

| Response | Meaning | Do |
|---|---|---|
| `already_recorded: true` | You already reported this bill — a replay | Nothing. Do not count it twice |
| `filing.filed: true` | Copy filed to Drive, original archived | Nothing. Filing is automatic |
| `filing.error` set | Bill is fine; the Drive half is not | Note it in the summary. **Do not save the bill again** |
| `409` | Usually two bills now exist for one invoice | **Stop.** Tell Linda. Do not retry |
| `422` | A human stamp is missing | Do not save this bill |

**If you are unsure whether a save landed, check BuilderTrend — not this
API.** Reporting the same bill id twice is harmless. Saving twice creates a
second bill, and the API will then refuse the second report with a 409, which
is the app catching a mistake that has already happened rather than
preventing it.

## 7. Stop and ask (SOP §7)

Stop, flag, and move to the next invoice when:

- The **vendor** is not found after reasonable searching
- The **cost code** has no clear match (try the number *and* a keyword first)
- The **job** could be more than one BuilderTrend record
- A **Drive folder does not exist** — never create one
- A **filename collision** has a different file size
- BuilderTrend rejects the save as a **duplicate bill number**
- A **Bill dialog is already open** that you did not open — leave it alone

```
POST /invoices/{invoice_id}/flag
{ "code": "stop_and_ask", "detail": "what you saw, in a sentence or two" }
```

Then **continue with the next invoice.** Leave the record in `approved`, do
not save a partial bill, and do not delete a draft you did not create.

`detail` is read by a person the next morning. *"Vendor search for 'Cefali'
returned two entries, one marked DO NOT SELECT"* is useful. *"Could not find
vendor"* is not.

## 8. Vendor names (SOP §6)

The queue already gives you `pay_to` with the BuilderTrend name. This table
is for recognising what you are looking at:

| On the invoice | In BuilderTrend |
|---|---|
| CalPortland / Superior Ready Mix | **Catalina Pacific** |
| White Cap | **White Cap Construction & Industrial Supply** (search "White Cap" *with a space*) |
| Cefali & Associates | **Cefali & Associates,Inc** (no space) |

**Never pick a vendor labelled `** DO NOT SELECT **`.** The queue blocks
invoices whose recorded vendor is one of those, so if you see it on screen,
something is wrong — flag it.

**Never create a new Sub/Vendor.** Ever. Flag instead.

## 9. White Cap

The app now splits combined White Cap files into one invoice per page before
you ever see them, so each queue item is a single invoice on a single job.
Yard invoices never reach the queue — they are flagged at intake and never
entered.

Credit memos arrive with a negative `amount`. Enter them as a normal bill
with the negative amount, on the same job.

## 10. What the app will never let you do

Not restrictions to work around — they are the reasons the app exists.

- **Approve or mark reviewed.** Both require a person; the agent key is
  refused on both.
- **Change a cost code, amount, or any invoice field.** `PATCH` is signed-in
  only.
- **Create a vendor, cost code, project, or Drive folder.** Each is a
  stop-and-ask.
- **Delete a Drive file.** There is no delete call anywhere in the app.

## 11. End of batch (SOP §10)

Report, in the chat:

- Every invoice saved, with its BuilderTrend bill id
- Every invoice flagged, with the reason
- Any filing errors reported by `mark-uploaded`
- Anything learned worth adding to the project doc

The app's `/uploads` screen shows the same results, so the summary is for the
person reading the chat, not the system of record.
