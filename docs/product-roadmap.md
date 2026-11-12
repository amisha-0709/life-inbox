# Product improvement backlog

This is the team's continuing backlog, replacing earlier milestone completion
claims. Code existing does not establish public-beta readiness. The UI redesign pass delivers four stacked, reviewable PRs; it does not certify the whole product for public use.

**PRs #12 and #13 are merged**, along with the deadline-isolation follow-up #14.
Incremental organization is implemented on `codex/incremental-analysis`, based on
that merged `main`; it has no unmerged stacked dependency. No merge, deployment
or personal-database migration is part of this pass.

The UI redesign requirement map, checkpoints and evidence live in
[`redesign-checklist.md`](redesign-checklist.md). PRs #18 and #20 are merged;
the Inbox changes from #19 landed through #22. PR #23 now targets `main` with
only the remaining Ask/integration changes. Conflict-resolution checks use
disposable data; no personal-database migration is part of this work.
The next quality scope is documented
in [`extraction-quality-followup.md`](extraction-quality-followup.md).

## Users and journeys

Assumption: the first user is an individual who captures personal obligations and
reference material throughout the day, then returns to find information and decide
what to do. Multiple people may use a shared beta deployment, so privacy is a
prerequisite. No interviews or representative usability study have validated this.

The core journeys are capture → saved original → processing → review/correction;
find an item through filters/search/Ask; and turn a sourced task into a deliberate
action. The current stack is FastAPI, SQLAlchemy/Alembic, HTML and vanilla JS.

## Evidence from the configuration-safety baseline (PR #12)

- Read `AGENTS.md`, `README.md`, `BUILD_SPRINT.md`, `docs/architecture.md`, routes,
  persistence, browser code and relevant tests. Existing untracked database
  backups were left untouched; smoke checks used disposable databases.
- `env -u OPENAI_API_KEY .venv/bin/python -m pytest -q --tb=short`:
  **133 passed, 3 failed** in the network-restricted environment. The failures were
  URL capture/extraction, retry and URL-in-analysis tests in `tests/test_api.py`.
  They mock fetching but retain DNS validation in
  `app/capture/url_fetch.py::_host_is_blocked`; treat this as a test-environment
  dependency, not evidence that URL capture fails in an ordinary browser.
- Observed with a temporary SQLite database and controlled clock: a note captured
  October 15 saying “Pay the electricity bill today” generated an October 15 task.
  On October 20 it was overdue; re-analysis changed the same task to October 20 and
  removed it from Overdue. Confirming the original wording also selected October 20.
- At that baseline, `app/attention/dates.py::parse_timing_phrase` raised for
  `2026-02-30`, interpreted “urgent tomorrow” as today and reduced “tomorrow at
  10am” to a date without acknowledging the lost time. Those reproductions are
  now regression cases in the deadline PR.
- A real local Uvicorn smoke check with a migrated disposable database exercised
  text/PDF/image/voice capture, invalid input, text correction, analysis, cited Ask,
  confirmed dates, Attention, mock calendar confirmation and task completion.
  Saved content, date corrections and completion survived a server restart.
  Headless Chrome screenshots at 1440×1100 and 390×844 confirmed rendering; the
  mobile Tasks filter and panel content were visibly clipped horizontally.

## Ranked work

Effort is an approximate engineering estimate, including tests; each row still
needs a focused scope before implementation. Evidence below is code inspection
unless the baseline above records an execution.

| Rank / status | User problem and evidence | Expected value | Dependencies | Effort |
| --- | --- | --- | --- | --- |
| 1 — **Merged, PR #12** | Baseline beta startup accepted missing secrets and SQLite; the signer permitted the published local secret. Lifespan now validates before serving; authentication validates nonlocal configuration, rejects malformed session claims and consistently uses secure nonlocal cookies. | Protect private inboxes by refusing unsafe beta/production settings. | Local defaults preserved; startup/auth/isolation tests and operator guidance updated. | Delivered |
| 2 — **Merged, PR #13** | Delayed/repeated processing used to move relative dates. Captures/edits now save a reference and timezone; tasks save date-only provenance and reuse unchanged source versions. Tasks and Attention expose edit/clear; calendar uses saved dates. See `docs/deadlines.md`. | Trust the deadline from capture through correction, Attention and calendar preview. | Migration 0014; follow-up isolation coverage merged in #14. | Delivered |
| 3 — **Implemented in this PR** | Unchanged analysis previously resent the full inbox. Persistent per-item state now selects only deltas; bounded context and validated proposals preserve unrelated rows and attach trustworthy provenance. | Lower model usage and stabilize saved organization/tasks while preserving choices. | Migration 0015; see `docs/incremental-analysis.md` and evidence below. | Focused incremental contract |
| 4 — **Delivered in the UI redesign stack** | Item details now persist title/topic/priority corrections before analysis, and task details persist separate user text/priority choices. Saved deadline controls and undo remain available. | Let people repair mistakes and understand which values are confirmed. | Expose insight overrides and track task user edits; preserve the new eligibility and deadline contracts. | 2–4 days |
| 5 — Partly delivered | Attention now has deadline editing/preview actions and no silent eight-row cap; affected mobile overflow is fixed. Direct completion, source navigation and retained stale-action review remain limited. | Help people finish or investigate an obligation directly from the daily view. | Keep desktop/mobile deadline controls accessible; design source-change recovery. | 1–2 days |
| 6 — Partly delivered | `fetchJson` now shows useful server error detail. PDF extracted-text correction is still allowed by API but absent from browser controls; capture/detail flows remain uneven. | Recover from extraction mistakes without recapturing. | Review source-specific controls with representative failure fixtures. | 2–3 days |
| 7 | Calendar drafts live in process memory (`app/integrations/calendar.py::_DRAFTS`); `app/database.py::save_calendar_receipt` inserts another receipt on repeated confirmation. | Reliable preview → confirm → receipt, without duplicate actions. Consequence is currently limited by mock-only writes. | Persist owner-scoped drafts/idempotency and recovery; retain explicit confirmation and mock mode. | 2–4 days; real integration separate |
| 8 — **Next recommended: quality evaluation** | Retrieval builds passage vectors per request (`app/retrieval/search.py::search_passages`); quality/latency at realistic inbox sizes have not been measured. | Find useful evidence as inboxes grow. This is a scaling hypothesis, not a demonstrated need for a vector database. | Relevance fixtures and latency measurements before caching, indexing or reranking. | Evaluation 1–2 days; implementation depends on results |

## Deadline delivery evidence

The starting branch (PR #12, `ad9d44a`) passed **158 tests**. The current deadline
implementation adds persisted reference/provenance, conservative migration, strict
date-only editing/clearing, uncertainty handling and saved-date consumers.
`docs/deadlines.md` defines source edits, unchanged suggestions, user overrides,
multi-source uncertainty, retained stale actions and legacy compatibility.

Controlled-clock integration checks cover delayed first analysis, later analysis,
read/confirmation/restart persistence, corrections and clearing, source edits,
UTC/Kolkata midnight, both New York DST transitions, malformed dates, unsupported
exact times (including dotted `a.m.`), ambiguous candidates, legacy records,
multi-source context, concurrent edits and two-user isolation. Regression review
also caught and fixed duplicate tasks after short action edits, removed actions
and changed model wording/source grouping.

The targeted source-edit review reproduced a remaining bug: changing ₹2,000 to
₹2,100 on October 20 renewed an October 15 “today” deadline. PR #13 now compares
timing intention per obligation instead of treating every content edit as a
reschedule. Focused regressions cover amount changes before/after timing wording,
typos, sibling edits, intentional timing changes, ambiguity and all explicit user
choices. See `docs/deadlines.md` for the actual policy and stacked-PR integration
history; this was a correction within PR #13, not a new roadmap workstream.

A real Uvicorn + headless Chrome walkthrough passed at **1440×1100 desktop** and
**390×844 mobile**: capture October 15 → first analysis October 20 → inspect and
confirm October 15 → edit to October 25 → re-analyse → matching Attention/calendar
October 25 → clear → re-analyse still undated. DOM checks found no horizontal
overflow; editor and calendar screenshots were visually inspected. All data and
browser profiles were disposable. No personal inbox database was migrated.

## Incremental organization evidence

The starting suite passed **293 tests** before edits. The branch was updated to
include merged PR #14. New checks cover durable eligibility after restart,
zero-call unchanged/empty runs, one-note deltas, explicit invalidation, source
changes during planning, concurrent claims/corrections, bounded batches/oversized
sources, targeted lookups and incomplete-context recovery, legacy bootstrapping,
ownership, duplicate captures, sourced detail, distinct/recurring occurrences,
ambiguous matches and preserved user choices. Saved runs contain deltas rather
than regenerated global lists. All prior deadline checks remain in the suite.

Review found and fixed same-source occurrence conflation, namespace/account ID
collisions, reference digits masking changed amounts, vendor/deposit/balance
ambiguity, lost context-omission metadata, and overly broad targeted lookups.
Matching stays conservative; this is regression evidence, not a representative
semantic-retrieval evaluation.

A real Uvicorn + headless Chrome flow used a migrated disposable database and
20 already processed captures. These are measured no-key fallback results:

| View | New analysis targets | Existing tasks / snippets | Context JSON characters | Organizer-model calls / consumed input tokens |
| --- | --- | --- | --- | --- |
| Desktop 1440×1100 | 1 | 4 / 4 | 3,210 | 0 / 0 |
| Mobile 390×844 | 1 | 5 / 5 | 3,960 | 0 / 0 |

The browser verified delayed first analysis, a zero-target/zero-call unchanged
rerun, explicit invalidation, preserved deadline confirmation/edit/clear,
Attention and calendar consistency, uncertain-match review decisions, and no
horizontal overflow. Calendar/editor screenshots were visually inspected. Provider usage
handling is tested with a mocked SDK response; no live model cost or tokenizer
measurement is claimed. Character/4 estimates are explicitly labeled separately
from provider-reported token usage. See `docs/incremental-analysis.md` for the full
contract, budgets, retry behavior and validation commands.

Final automated check:
`env -u OPENAI_API_KEY .venv/bin/python -m pytest -q --tb=short --show-capture=no`
— **444 passed**. Empty and legacy disposable databases migrate to 0015 in the
suite; `node --check app/static/app.js` and `git diff --check` also pass. No new
dependency was added. Existing personal database files/backups were untouched.

## Delivery boundary

This PR makes organizer targets, model context and persisted reconciliation
incremental. Ask/transcription are separate. It does not add a background
scheduler, semantic retrieval, exact instants, external calendar writes, deployment
or merges. Candidate selection still scans local owned records. Review can link
provenance or keep tasks separate; it does not destructively merge existing task
identities. Arbitrary source rewrites can leave retained stale tasks for manual
review. PostgreSQL concurrency and live provider quality/cost remain unverified.
Validate the next correction-controls slice with users before expanding scope.

Deadline baseline check: `env -u OPENAI_API_KEY .venv/bin/python -m pytest -q --tb=short`
— **293 passed**, including 32 source-edit regressions from the targeted review.
Empty and existing legacy databases upgrade to
0014 in tests; missing historical reference/timezone fields remain NULL and saved
dates/confirmations remain intact. `node --check app/static/app.js` and
`git diff --check` pass. No dependency was added.

Public-beta checks still outstanding include live Google consent, a real PostgreSQL
connection and provider URL compatibility, durable hosted uploads, and Google
profile verification/account-linking edge cases. No paid API or live Google flow
was used during this pass.
