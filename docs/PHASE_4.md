# Phase 4 — White Cap splitting, duplicate hardening, metrics

Phase 4 is the cleanup phase: the three things Phases 1–3 deferred with a
note saying "later", plus the admin gap those notes created.

---

## 1. White Cap combined files (SOP §5)

White Cap sends one PDF covering many jobs. The SOP is unambiguous: **one
page is one invoice.** Until now a combined file was flagged with a message
saying the splitter was a later phase.

Reading a twelve-page bundle as one document is not a failure that announces
itself. It succeeds, producing one confident five-figure total on one wrong
job — which gets paid.

```
combined.pdf (12 pages)
   │
   ├─ page 1 → invoice, source_page=1  ─┐
   ├─ page 2 → invoice, source_page=2   │  all sharing source_file_id
   └─ …                                 ┘
```

The `(source_file_id, source_page)` unique index from migration 001 existed
for exactly this, so re-polling a combined file re-finds every page instead
of creating twelve more every fifteen minutes.

**After the split there is no White-Cap-specific path.** Same extraction,
same flag reasons, same states. A second extraction path would be a second
set of rules to keep correct, and only one of them would be exercised.

Three things are specific, and all three are SOP rules rather than code
conveniences:

- **Yard pages never become payables** (SOP §5). Caught before the Claude
  call when the page has a text layer, because recognising one costs nothing
  and should not cost a model call either. Scanned pages have no text, so the
  existing post-extraction check still catches those.
- **The CUSTOMER JOB NO. line is handed over as a hint.** It stays a hint:
  `resolve_project` still has to match it, and an unmatched job flags rather
  than guesses.
- **The combined original is one Drive file behind many invoices**, so filing
  does not archive it until every sibling is filed or voided. Moving it when
  page one is done hides a document whose other pages are still being worked.
  Voided counts as settled — otherwise one yard page would hold every
  combined original in the intake folder forever.

Two failure modes are deliberately bounded. One bad page does not abandon the
other eleven, because each page is a separate payable. And a page cap refuses
absurd bundles: without it, one malformed scan turns a poll run into hundreds
of Claude calls before anybody looks at it.

## 2. Duplicate detection

Phase 1 matched on exact equality — same vendor and invoice number, or same
vendor, amount and date — and Phase 1's own notes recorded the gap: *"a
re-scan that reads `2294.25` as `2294.26` slips through both tests."*

That is not an exotic case. It is the most ordinary thing that happens to
these documents, and the result is a vendor paid twice, discovered in a
reconciliation weeks later or not at all.

**The asymmetry decides every judgement call here.** A missed duplicate is a
double payment. A false one is thirty seconds of Linda's time. So it errs
toward flagging — but not so loosely that the queue fills with noise, because
a queue nobody trusts is a queue nobody reads, which converts the cheap error
back into the expensive one.

| Signal | Weight | Why that weight |
|---|---|---|
| Byte-identical PDF | decisive | Proof, not evidence. Holds when every extracted field differs because the second scan was worse |
| Invoice number | 3 | Two invoices from one vendor sharing a number essentially do not happen |
| Amount | 2 | Within a misread. Never flags alone |
| Date | 1 | One vendor bills one project repeatedly; alone this would flag most of the queue |

Three is the flag threshold, so a number alone is enough, amount-and-date
together is enough, and a date alone is not.

**Matching is tolerant in the ways scans are wrong.** Invoice numbers
normalise to their digits, so `278461-1`, `INV-278461` and `278461` are one
bill wearing three coats — with a length floor so `12` does not match `1123`.
Amounts match within a misread of the cents and dimes. Dates match within a
few days.

**The identical-PDF check runs first, unscoped and unwindowed.** Everything
else narrows by vendor and by a 90-day window, which are good heuristics but
still heuristics. Filtering proof through a heuristic means a re-scan dated a
year earlier, or one ingested before its vendor resolved, is missed.

**Every match carries its reason.** "Possible duplicate" alone sends somebody
to open two PDFs; "same invoice number, and the amount is 1 cent apart" is
usually decidable without opening either.

## 3. `/metrics` — does the AI deserve the trust?

§12: *"Add a small `/metrics` page showing AI acceptance rate by cost code
and vendor."*

Measured against invoices a person has **approved**, because that is the only
point where somebody has definitely looked. Anything earlier scores the model
against itself.

- **By vendor** — White Cap is always 3015 and should be near perfect.
  Ready-mix is the hard case the whole §8 authority order exists for; a lower
  rate there is expected, not alarming.
- **By cost code** — bucketed by what the AI *said*, not what was kept,
  because the question is "when it says 3002, is it right". A code rejected
  more often than not usually needs better element keywords.
- **By confidence band** — the one that decides things. If "high" is accepted
  no more often than "low", the score is decoration, and every UI decision
  resting on it is resting on nothing. The page says that in those words when
  the data shows it.

Three judgements that would otherwise quietly mislead:

- **Counted per invoice, not per cost row.** A reviewer who accepts the
  suggested code and then splits the invoice across two codes has accepted
  the suggestion; counting rows scores that 50%.
- **An invoice with no suggestion is excluded, not a miss.** The model never
  made a claim, so scoring it against one invents a failure.
- **No data is `—`, not 0%.** Zero percent reads as the model failing. Low
  volume stays neutral-coloured too: a red pill on two invoices reads as a
  verdict.

## 4. Admin

The gap Phase 4 created and then closed: `/metrics` tells you a weak cost
code needs better element keywords, and `/admin` displayed those keywords
read-only. The backend had accepted the edit since Phase 0; only the UI was
missing. They are now editable inline, as a comma-separated list, saving on
blur like every other reference field.

The code string itself stays uneditable. `invoice_costs` and `mix_designs`
rows point at a cost code by id and the code string is what Chrome types into
BuilderTrend, so renaming one in place would silently change what a past
approval meant. A BuilderTrend rename is handled by deactivating the old code
and adding the new one.

## 5. Running the gate

**White Cap split.** Drop a multi-page White Cap PDF in `White Cap/`. One
invoice appears per page, each with its own PDF on the detail screen. Yard
pages land in `/flagged` under "yard". Re-run intake: nothing is created the
second time.

**Duplicates.** Take an invoice already in the system, re-scan it (or edit a
cent), and drop it in. It flags as `possible_duplicate`, and the detail says
which signals matched.

**Metrics.** Approve a handful of invoices, then open `/metrics`. With fewer
than ten in a confidence band it will decline to draw a conclusion, which is
correct.

## 6. Known gaps to pick up later

- **Split pages are not written back to Drive.** SOP §5 has a human splitting
  into `White Cap/[Job Name]/`; the app keeps each page in Storage instead and
  never creates a Drive folder (§12). The Drive split was a workaround for a
  manual process, but anyone looking in Drive will still see only the
  combined file.
- **Yard detection before extraction needs a text layer.** Scanned pages fall
  through to the post-extraction check, which costs a Claude call per yard
  page.
- **The duplicate scan reads up to 500 of a vendor's recent invoices per
  check.** Fine at this scale, and indexed, but it is a per-invoice query that
  grows with vendor history.
- **Metrics has no trend over time.** It answers "how are we doing" and not
  "are we getting better", which is the more useful question once there is a
  year of data.
- **No Sentry and no structured JSON logging**, both still outstanding from
  §12. Every 500 carries an `error_id` that also appears in the Render logs.
