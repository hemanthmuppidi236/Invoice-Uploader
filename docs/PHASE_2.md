# Phase 2 — assignment, review, approval, emails

Phase 2 is the part the whole app exists for: moving the cost-code decision
from accounting guessing after the fact to the project engineer who poured the
concrete, with an approver behind them.

The Phase 2 gate from the build spec: **run one invoice through all three
people end to end and receive both emails.** Everything the app needs for that
is built.

---

## 1. The state machine

```
ingested ─ai─▶ suggested ─assign─▶ assigned ─reviewed─▶ pending_approval
                                      ▲                        │
                                      └───── reject ───────────┘
                                                               │ approve
                                                               ▼
                                                            approved
```

| Transition | From | To | Who |
|---|---|---|---|
| `assign` | ingested, suggested, flagged | assigned | admin, accountant |
| `reassign` | assigned, pending_approval | *(unchanged)* | admin, accountant |
| `mark-reviewed` | assigned | pending_approval | the assigned reviewer, or admin/accountant |
| `approve` | pending_approval | approved | the assigned approver, or admin |
| `reject` | pending_approval | assigned | the assigned approver, or admin |

Every transition goes through one guard in `state_machine.py`, which does the
three things §6 requires — validate the current state (409 on mismatch), stamp
the actor and a UTC time, write `audit_log` — so a new transition cannot
forget one of them.

`assign` accepts a `flagged` invoice on purpose. Linda routinely clears a flag
by setting the project or vendor and handing it straight to a reviewer;
forcing an unflag first would be a click for no gain.

### Preconditions

**`mark-reviewed` is the certification gate.** Past it the next human decision
is "approve", and after that the Chrome session types these values into
BuilderTrend. So it requires a project, a vendor, an amount, an invoice date,
an invoice number, a named approver, and a cost split that sums to the total.
A missing job or vendor at BuilderTrend time is a bill on the wrong record;
catching it here means somebody is still looking at the invoice.

**`approve` requires a review stamp and re-asserts the balance.** No current
path can unbalance a split in between — `PATCH` is refused once the invoice is
past review — but `approved` is the one state the Chrome session reads, so the
invariant is re-asserted at that gate rather than inherited from a check that
ran earlier.

**`reject` clears `reviewed_at`.** Without that, an invoice sent back could be
approved again without a second review, because `require_reviewed` only checks
that the stamp exists.

### Who can act on a record

The role check on the route says "could be a reviewer". A second, record-level
check says "is *this* invoice's reviewer".

- **Review**: the assigned reviewer, or an admin, or an accountant. Another
  project engineer is refused — the audit trail has to record that the person
  asked is the person who certified it.
- **Approve and reject**: the assigned approver, or an admin. Another approver
  is refused *even though they hold the role*, because the queue is "pending
  MY approval" and letting any approver clear any invoice would make the
  approver field decorative. If the assigned approver is away, accounting uses
  `reassign`, which is recorded.
- **The Chrome session can do neither.** §12 requires a human stamp at both
  gates, so the agent key is rejected on both with an explanation rather than
  a bare 403.

---

## 2. The two emails

Both go out through Render cron against authenticated endpoints, honor the
`JOBS_PAUSED` kill switch, and skip weekends by default (§14).

### §9.1 — Daily "in your court", 3:30 PM Pacific

One email per person who has at least one invoice waiting on them: `assigned`
if they are the reviewer, `pending_approval` if they are the approver. **Anyone
with nothing pending gets nothing.** A digest that is usually empty stops being
opened, and then the one that matters gets ignored with it.

Someone holding both roles — Linda — gets two emails rather than one merged
list. The two piles need different actions, and one table would bury which is
which.

The table carries vendor, project, amount, the suggested code with its
confidence in the same colour the app uses, and days waiting. Every row links
to the invoice; a button links to the queue.

An invoice in a waiting state with *nobody* assigned appears in no digest at
all, so it would sit indefinitely with no signal. The job reports those and
emails the admin about them.

### §9.2 — End of day, 5:30 PM Pacific

To the distribution list, **only if at least one invoice was approved that
day.** Grouped by project, with vendor, invoice number, amount, cost code,
reviewer, and approver per row, and a footer counting approved today, uploaded
today, still pending, and flagged.

"Today" means the working day in `EMAIL_TIMEZONE`, not a UTC window —
otherwise invoices approved late in the afternoon would land in tomorrow's
summary.

### Send-once idempotency

A double cron fire must not double-mail. The mechanism is `email_log`:

1. **Insert the log row first**, claiming the slot. A unique index on
   `(kind, recipient, created_at::date) WHERE error IS NULL` means a second
   attempt for the same person, same kind, same day cannot insert.
2. **Send.**
3. **On success**, stamp `sent_at`. The slot stays held.
4. **On failure**, write the error — which drops the row out of the *partial*
   index and frees the slot, so a retry can legitimately try again.

That ordering is the whole design. Claiming after sending leaves a race where
two runs both send. Claiming without releasing on failure makes a transient
Gmail 503 permanent for the rest of the day.

Failures alert the admin (§9), and the alert itself goes through the same
claim mechanism under kind `admin_alert`, so a job failing repeatedly produces
one alert per day rather than one per attempt.

---

## 3. Setting up email

Same single-user OAuth as the pay app. Gmail's HTTP API rather than SMTP,
because Render blocks outbound SMTP on every plan.

1. In Google Cloud, create an OAuth client (Desktop app) and enable the Gmail
   API.
2. Get a refresh token once. The pay app's
   `scripts/get_gmail_refresh_token.py` does this — the only scope needed is
   `gmail.send`.
3. Set the backend environment variables:

```bash
EMAIL_PROVIDER=gmail_api
GMAIL_OAUTH_CLIENT_ID=...
GMAIL_OAUTH_CLIENT_SECRET=...
GMAIL_OAUTH_REFRESH_TOKEN=...
GMAIL_SENDER_EMAIL=noreply@ferrocretebuilders.com   # must match the consenting account
ADMIN_ALERT_EMAIL=you@ferrocretebuilders.com
APP_URL=https://your-app.vercel.app                 # deep links in the mail
```

`GET /health` reports `"email": true` once all four OAuth values are present.
Until then `EMAIL_PROVIDER=log_only` is the default and the jobs log what they
*would* have sent — a legitimate configuration, and the `email_log` row
distinguishes "logged" from "sent".

### Render cron

```
# §9.1 — 3:30 PM Pacific, weekdays. Cron is UTC; 22:30 UTC is 3:30 PM PDT.
Schedule: 30 22 * * 1-5
Command:  curl -fsS -X POST "$API_URL/jobs/email-daily" -H "X-Agent-Key: $AGENT_API_KEY"

# §9.2 — 5:30 PM Pacific, weekdays.
Schedule: 30 0 * * 2-6
Command:  curl -fsS -X POST "$API_URL/jobs/email-eod" -H "X-Agent-Key: $AGENT_API_KEY"
```

Two things about those schedules worth stating plainly:

- **Render cron is UTC and does not observe daylight saving.** The expressions
  above are correct for PDT (UTC−7). Under PST (UTC−8) they fire an hour
  early, at 2:30 PM and 4:30 PM Pacific. The app's own weekday guard uses
  `EMAIL_TIMEZONE` so the day boundary stays right either way; only the
  *time of day* drifts. Either accept the hour, or shift the expressions twice
  a year.
- **The EOD job crosses midnight UTC**, which is why its day-of-week range is
  `2-6` rather than `1-5`: 5:30 PM Pacific Monday is 00:30 UTC Tuesday.

Test either job without waiting for the clock:

```bash
curl -X POST "$API_URL/jobs/email-daily?force=true" -H "X-Agent-Key: $AGENT_API_KEY"
```

`force=true` bypasses the weekday and once-per-day guards. It still writes a
log row, so a forced send is visible rather than invisible.

---

## 4. Running the gate

1. Get an invoice to `suggested` (Phase 1), or set one up by hand.
2. **As Linda** (`accountant`): open it, confirm the reviewer and approver, and
   press **Assign for review**. Or tick several on `/invoices` and use bulk
   assign.
3. **As the PE** (`pe`): the invoice is in your court on `/invoices`. Check the
   values against the PDF, fix anything wrong, confirm the cost code, pick an
   approver, press **Mark reviewed**. The panel tells you what is missing if
   it will not let you.
4. **As Raz** (`approver`): it is now in your court. Press **Approve** — or
   **Reject** with a reason and watch it land back with the PE, the reason in
   their banner and the review stamp cleared.
5. Fire both emails with `force=true` and check them.

Worth trying deliberately: approve as the *wrong* approver (a 403 naming the
problem), mark reviewed with an unbalanced split (a 422 naming the gap), and
approve an invoice someone else already approved in another tab (a 409 telling
you to reload).

---

## 5. Decisions taken in Phase 2

- **No per-transition emails.** §14: "Approvers get the same daily digest as
  reviewers, no immediate pings." The two scheduled digests are the only
  notifications, which is why the state machine has no notify hook.
- **Another approver cannot clear someone else's invoice**, even holding the
  role. Reassignment is the supported path and it is recorded. The alternative
  makes the approver field decorative and the audit trail misleading about who
  was asked.
- **The reviewer sets the approver in the same call as marking reviewed.** They
  know who should sign off, and making them save a field and then click a
  button is two steps for one decision.
- **Bulk assign reports per invoice, not all-or-nothing.** One row that moved
  state since the page loaded should not block the other nineteen, and the
  caller needs to know which one it was.
- **An approver must hold the `approver` role to be assigned as one.** Assigning
  to somebody who cannot approve parks the invoice in a queue nobody can clear,
  and the only symptom is an invoice that never moves.
- **Auto-save covers the cost split only**, 900 ms after typing, and only while
  the invoice is the reviewer's or Linda's turn (§10). Reference fields save on
  blur instead — a stray keystroke in a project dropdown should be
  recoverable, and an amount should not sit unsaved.
- **The client duplicates the server's preconditions** so the reviewer sees the
  same message before the click that they would get after it. The server copy
  is the guarantee; they are kept in sync by hand.

## 6. Known gaps to pick up later

- **Render cron does not follow daylight saving**, so both jobs run an hour
  early from November to March. See above.
- **The auto-save has no conflict detection.** Two people editing the same
  invoice's split will overwrite each other, last write winning, with no
  warning. The audit log records both, so it is recoverable but not obvious.
  The pay app has the same characteristic.
- **`_lookup_maps()` in the email jobs loads every user, project, vendor, and
  cost code** on each run. Fine twice a day at this scale.
- **A rejected invoice keeps its original `assigned_at`.** Arguably correct —
  it was assigned then — but it means the "days waiting" figure in the digest
  counts from the first assignment, not from the rejection.
- **No per-person email preferences.** Everyone with pending work gets the
  digest; the only opt-out is the distribution list's `active` flag, which
  covers the EOD summary only.
