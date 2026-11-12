# Deadline behavior

## Reference and timezone

New captures save `time_reference_at` as a server-generated UTC instant and
`time_reference_timezone` as an IANA timezone. The browser sends its device
timezone in `X-Life-Inbox-Timezone`; API clients can send that header explicitly.
When absent, `LIFE_INBOX_TIMEZONE` is used (default UTC). An invalid header returns
400. Timezone context is reset after each request and is never a global user
preference. The capture screen identifies the device timezone being used.

Relative words mean the local calendar day of that recorded instant in that
recorded timezone. “Today” captured October 15 remains October 15 when first
analyzed October 20. No relative phrase is interpreted during a task read,
Attention read or calendar preview.

Content revisions and timing intention are separate. `updated_at` records source
edits, but an amount change, a non-temporal typo or an untimed sentence does not
renew the timing reference. When the recognized timing expressions change, the
source records the edit instant and editing request's timezone as potential new
context. Existing task deadlines are reconciled individually as described below;
one obligation's timing edit does not re-anchor its siblings.

No-op edits, automatic extraction, retries, generated titles and ordinary reads
preserve timing context. Original capture time remains in `created_at`. If mixed
timing edits happen before the first analysis, per-task provenance does not yet
exist: dates require review rather than assigning the edit clock to every older
relative phrase in that source. Unrelated edits before first analysis still
preserve the original capture reference.

## Saved deadlines and processing

`due_on` is a date-only `YYYY-MM-DD` value, never midnight UTC or an exact instant.
Task timing also saves the source wording, reference instant, timezone, review
note and provenance:

| Status | Meaning |
| --- | --- |
| `inferred` | Date inferred from the source in its recorded context. |
| `confirmed` | Person accepted the existing saved date without reinterpreting it. |
| `edited` | Person entered a new deadline. |
| `cleared` | Person explicitly removed the deadline; analysis keeps it absent. |
| `legacy` | Saved before reference tracking; historical timezone is unknown. |

Source fingerprints cover original/extracted content and its timing reference,
excluding generated titles. Each capture separately records the last version
used for task generation. An unchanged version reuses task identities and saved
timing even when model wording, grouping or timing suggestions drift. The
[incremental organization contract](incremental-analysis.md) now skips unchanged
captures entirely, including model calls. Only changed or explicitly invalidated
sources can refresh their matched task presentation; supporting captures and
unrelated tasks stay unchanged.

After a source edit, matching obligations reuse tasks, including short typos and
changed date wording. Timing signatures compare recognized relative/date/time
expressions and qualifiers separately from amounts and other text:

- **Unrelated content edits:** retain the saved deadline, original timing wording,
  reference timestamp and timezone. Acknowledge the new source fingerprint only
  after re-analysis verifies unchanged timing. For example, a bill captured and
  analyzed October 15 with “today” remains due October 15 when its amount changes
  from ₹2,000 to ₹2,100 on October 20. Editing a different obligation does not move it.
- **Clearly changed timing:** resolve the new, unambiguous wording in the edit
  context. Changing “today” to “tomorrow” on October 20 means October 21. Equivalent
  resolved dates alone do not prove unchanged intent: “today” → “in 0 days” is a
  changed instruction and uses the edit context.
- **Ambiguous edits:** removed, misspelled, qualified, conflicting or unsupported
  timing, or an edit matching multiple obligations, retains the previous deadline
  and provenance and requires review. Calendar previews are blocked until the
  person confirms, chooses or clears a date. Re-analysis does not remove that flag.
  Newly unmatched obligations in edited text also require review when no reliable
  reference can be assigned; they do not inherit a guessed historical date.
- **Explicit user choices:** confirmed, edited and cleared deadlines are never
  overwritten by this reconciliation.

For explicit choices and ambiguous edits, `source_changed` warns that the
source differs from the version behind the person's choice. Editing or confirming
the deadline acknowledges the current source version. Removed or unmatched old
actions are retained with a source-change warning rather than silently deleted
or completed. Review them manually; arbitrary action rewrites are not guaranteed
to match an existing task. This conservative retention can leave stale obligations.
Matching is a conservative text heuristic, not general understanding of intent.
Previously unrecognized temporal words have no trustworthy inferred date to
preserve; an edit can be indistinguishable from adding new timing. Use the date
control when correcting such wording or intentionally rescheduling without changing
recognized timing words. Complex paraphrases can require manual review.

Analysis checks its input versions before saving and returns 409 if a source was
edited or deleted while the organizer ran. No partial task/insight result is saved.
PostgreSQL source rows are locked through the save; PostgreSQL concurrency itself
has not been tested here.

## Uncertainty and calendar

Priority is separate: “urgent tomorrow” is tomorrow; urgency alone supplies no
date. Invalid dates, conflicting expressions, unsupported recurrence and exact
times stay unresolved with review guidance. A weekday, weekend or yearless date
may have a candidate with an explicit confirmation requirement. Exact times such
as “tomorrow at 10 a.m.” have no saved candidate: use the picker to deliberately
choose a date-only deadline. This feature does not schedule timed events.

Original source sentences are consulted so a model cannot silently turn
“tomorrow at 10 a.m.” into an unqualified “tomorrow”. If timing cannot be assigned
to an action, it requires review. Multiple sources with different reference days
or timezones also require review; no source is silently treated as authoritative.

Tasks and Attention display the saved date, source wording, provenance and review
guidance. Edit deadline is available on both; its date picker can save or clear a
deadline. Attention compares the saved calendar date to the viewing request's
local day; switching timezone does not modify saved dates. Snooze is separate.

Calendar previews use the saved date and explicitly identify date-only/all-day
semantics. Missing, uncertain or stale inferred dates are blocked. An explicit
`start_date` must equal the saved deadline; edit the task first to change it.
Confirming a draft after a deadline change returns 409 and asks for a new preview.
The browser also invalidates pending previews when dates, sources or owners change.
Calendar writes remain local mocks; durable drafts and receipt idempotency are
separate backlog work.

## API and migration

- `PATCH /api/tasks/{id}/deadline` with `{"due_on":"2026-10-25"}` edits a deadline;
  `{"due_on":null}` clears it. The field is required; datetimes and invalid dates
  return 422. Foreign task IDs return 404.
- `POST /api/tasks/{id}/confirm-timing` with `{}` or unchanged `timing_text` accepts
  the saved date. A missing candidate returns 400. Older callers entering new
  wording remain supported; it is interpreted at the correction instant and
  unresolved wording remains flagged for review.
- Apply Alembic revision `0014_deadline_provenance` with
  `python -m alembic upgrade head` before starting the updated app.

The migration preserves existing dates, wording, confirmed flags, statuses and
source links. Historical reference timestamps/timezones remain NULL. Saved,
unconfirmed legacy dates need review; confirmed valid dates remain usable.
Malformed saved dates are retained in storage but exposed as unresolved for
correction. Missing historical dates are never reconstructed using today's clock
or an assumed historical timezone. No personal database is needed for tests.
