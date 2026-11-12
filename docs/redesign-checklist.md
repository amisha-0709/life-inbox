# UI redesign implementation checkpoint

One implementation checklist for the attached redesign brief. Do not repeat the
repository audit on resume. Preserve the merged incremental/deadline contracts,
owner isolation, existing captures and task identities. Never use the personal
database for tests, merge PRs, deploy, or delete historical tasks for appearance.

## Shared decisions

- Existing FastAPI + HTML + vanilla JS stack; restrained green, compact typography,
  consistent 44px touch controls, visible keyboard focus, reduced-motion support.
- Primary destinations Inbox / Tasks / Ask; Settings under Account. URL is the
  navigation source of truth. One visible destination; reload and Back/Forward agree.
- Canonical saved collection and durable task state; no competing AI task reports.
- Desktop side details / mobile full-screen dialog; return preserves collection
  filters, loaded rows, scroll and trigger focus. Citations and provenance use it.
- Save original promptly. Only capture/edit/retry actions queue incremental
  organization; browsing/navigation/filtering never calls the organizer.
- Tests and screenshots use disposable data. Provider calls mocked or no-key.

## Requirement map and sequence

| Slice / branch | Requirements and acceptance | State |
| --- | --- | --- |
| 1 `codex/redesign-shell-capture` | Compact persistent shell; three destinations and Account/Settings; correct URL/history/reload; shared responsive/accessibility styles; unified composer text/standalone URL/files/drop/paste image/audio; previews/remove; recording start/stop/review/cancel; failure preserves unsaved input; single Save; truthful extraction and automatic delta status. | Delivered [PR #18](https://github.com/tmiiitd/life-inbox/pull/18), base `main`, CI passed |
| 2 `codex/redesign-inbox-details` | One compact paginated collection; search/topic/type/status/priority filters; desktop/mobile details and return position; original/media/extracted content/tasks/relations; durable title/topic/priority corrections before/after analysis; secondary re-analyse/delete; discoverable extraction retry. | Landed on `main` through [PR #22](https://github.com/tmiiitd/life-inbox/pull/22), carrying the original PR #19 changes |
| 3 `codex/redesign-tasks-review` | Today/Upcoming/All; overdue/undated/completed/review filters; compact authoritative rows; complete/undo/dismiss; discoverable deadline editing; source navigation; provenance/date warnings; focused failure/uncertainty/duplicate review, no permanent empty report cards. | Merged [PR #20](https://github.com/tmiiitd/life-inbox/pull/20) into `main`; integration verification below |
| 4 `codex/redesign-ask-integration` | Readable grounded answers; citations open saved details; consistent loading/empty/error/owner-switch states; integrated 150+ task, long text, file, voice, failure/date/duplicate fixture walkthrough; desktop/mobile/keyboard/focus/history/reload and screenshots. | [PR #23](https://github.com/tmiiitd/life-inbox/pull/23), base `main`; CI on linked PR |

Extraction-quality findings are tracked separately in
`docs/extraction-quality-followup.md`. Do not silently clean historical records.
Use a fifth PR only for a distinct remaining requirement, not an arbitrary split.

## Slice checkpoints

### 1 — Shell and capture

Branch `codex/redesign-shell-capture`; [PR #18](https://github.com/tmiiitd/life-inbox/pull/18), base `main`, commit `89de518`. **456 tests passed**, CI passed, desktop/mobile shell walkthrough passed. Before/after synthetic screenshots committed under `docs/screenshots/redesign/`. Process-local extraction and in-memory file drafts remain explicit limits.

### 2 — Inbox and details

Branch `codex/redesign-inbox-details`; [PR #19](https://github.com/tmiiitd/life-inbox/pull/19), stacked on PR #18; CI passed. **462 tests passed**, including migration 0016 empty/legacy preservation and owner isolation. Browser desktop passed with **166 captures / 164 tasks**; final mobile checks passed. Source/correction edits, re-analysis, stable task IDs/dates, media/extracted text, related/source navigation, failure retry, deep links, Back/Forward/reload, list position and modal keyboard focus were exercised. Screenshots were visually inspected.

Migration 0016 permits pending insight corrections without a fabricated analysis run. Topic/priority/search filters honor effective user values. Individual topic clearing remains the existing API limitation; Reset all corrections is explicit. Task/review and Ask integration remain slices 3 and 4.

### 3 — Tasks and focused review

Branch `codex/redesign-tasks-review`; [PR #20](https://github.com/tmiiitd/life-inbox/pull/20), stacked on PR #19; CI passed. **482 tests passed**,
including strict task corrections, dates/identity/completion, ownership, migrations,
and a deterministic concurrent processing-state bootstrap regression. Real Chrome
Task checks passed desktop and mobile with **164 tasks**, reaching every row via
40-row loading. Verified Today/Upcoming/filters, complete/undo, text/priority/date
choices through re-analysis, clear, source/Back, uncertain-date correction,
duplicate decisions, capture retry, keyboard focus and visible calendar preview.

Migration 0017 adds user text/priority overrides without rewriting inference.
Task metadata is loaded as an owned collection and paged in the browser; unbounded
API scale is not claimed. URL/transcription are fixture-backed; no paid calls.
Ask and the final combined walkthrough were completed in slice 4 below.

### 4 — Ask and integrated verification

Branch `codex/redesign-ask-integration`; [PR #23](https://github.com/tmiiitd/life-inbox/pull/23), now based on `main` after PR #20 merged. Final implementation commit `e90f2f0`; follow-up documentation records the PR links. CI is tracked on that PR. Readable answers and
working source links use the same item detail; per-owner draft/answer recovery
makes no repeat Ask requests. Failed questions retain input and the prior answer;
changed captures mark cached answers stale. Retrieval titles honor user corrections.
Repeated extractive passage wording is deduplicated without merging saved captures.
Owner switches clear old content, filters, modal/review state and pending responses.

Final automated suite: **484 tests passed**. Browser JS/Python syntax and diff checks pass. Full real-Chrome
walkthrough passed **1440×1000 desktop and 390×844 mobile**, initially **167 captures
and 164 tasks**, then additional captures. All tasks were reachable through 40-row
loading. Covered unified capture (including real MediaRecorder with a fake device),
draft failures, automatic organization, filters/details/history/reload/list position,
corrections and dates through re-analysis, completion/undo/dismissal/restore, source
navigation, uncertain/duplicate/failed processing review, calendar preview, Ask
citation/history/cache, insufficient evidence, failed Ask and actual local account
switching. No browser exceptions. Saved text/priority/date/completion remained stable.

Measured full-run HTTP POST counts were **31 analysis requests / 9 Ask requests**
across explicit capture/correction/question scenarios. These are HTTP counts, not
model calls. No provider key was set, so actual organizer/Ask provider calls and
consumed provider tokens were **zero**. Citation navigation, history, reload and
owner restoration added zero Ask/organizer POSTs. Provider quality/cost, real
microphone hardware and live URL/transcription services were not verified.

Evidence: committed before/after PNGs and `browser-results.json` under
`docs/screenshots/redesign/`. Reproduce with
`python scripts/redesign_smoke.py --stage all --full`; focused stage/viewport flags
are available. Latest mobile-all additionally verified dismiss/undo/restore and
that additional task IDs require genuinely new sources. A fresh mobile Ask run
verified concise deduplicated answers after that final readability fix.

Known limits: extraction is process-local, file drafts are in memory, task metadata
is fetched as an owned collection then paged client-side, and clearing one topic
field individually is not supported (Reset all corrections is explicit). The
concrete reference-to-task and Ask relevance problems remain separately scoped in
`docs/extraction-quality-followup.md`; this UI delivery does not claim semantic
quality or public-beta readiness. No fifth UI PR is required by the verified scope.

### PR #20 integration with main — 30 October 2026

PR #20 was integrated against `main` with commit `3c537c0`, then merged as
`3d9ed1a`. The Inbox changes originally developed in PR #19 landed through
PR #22. Integrating `cc7ca45` reconciled the overlapping history while preserving
task/review behavior and main's privacy page, PostgreSQL migration/CI fix and
timezone dependency. The resulting PR diff contained only the remaining
task/review changes; original branch history was retained without a force push.

Verification after integration: **484 tests passed** with
`env -u OPENAI_API_KEY python -m pytest -q --tb=short --show-capture=no`.
`python scripts/redesign_smoke.py --stage tasks --full` passed real Chrome
desktop and mobile, starting with **166 captures / 164 tasks**, reaching every
task through 40-row loading and exercising corrections/re-analysis, dates,
completion/undo, source/history, review/retry and keyboard/layout. No browser
exceptions occurred. Empty temporary SQLite upgrade to migration 0017 and a
repeated upgrade passed; browser JavaScript syntax and diff checks passed.
URL/transcription use local fixtures, with no provider calls. No personal
database was used or migrated; that conflict-resolution task did not merge a PR
or initiate a deployment. PR #20 was merged subsequently.

### PR #23 integration with main — 30 October 2026

The Ask branch now incorporates `main` at `3d9ed1a`, including merged PR #20.
Only this checklist conflicted: its delivery/next-step notes were reconciled with
the newer task integration record. Ask and all other application code remain
identical to the original PR #23 implementation; main's privacy, PostgreSQL and
timezone fixes are retained. The remaining PR diff contains no duplicate task
backend or migration changes, and original branch history is preserved.

After integration, **486 automated tests passed**. The complete
`python scripts/redesign_smoke.py --stage all --full` walkthrough passed desktop
1440×1000 and mobile 390×844, starting with **167 captures / 164 tasks** and
covering capture, corrections/re-analysis, completion/undo/dismissal, dates,
review/retry, Ask citations/history/cache, failures and account isolation. There
were no browser exceptions. Measured HTTP counts remained **31 analysis / 9 Ask**;
provider calls were zero. Browser-module/smoke-script syntax, independent diff
review and whitespace/conflict-marker checks passed. Fresh Ask/source screenshots
were visually reviewed. All data/profiles were disposable; no personal database
was migrated, no PR was merged and no deployment was initiated by this work.

## Current checkpoint

- Baseline: merged `main` `f743db5`; existing untracked `data/*.backup*` untouched.
- Read: AGENTS/README/BUILD_SPRINT, template/styles/browser code, capture and
  correction routes and incremental contract once. Baseline findings (resolved):
  URL/navigation disagreement, synchronous extraction, missing pre-analysis
  corrections, filters ignoring overrides and unpaged task presentation.
- Verification: baseline **445 passed** before edits; deferred capture adds 11 passing cases.
- Slice 1 verification: **456 tests passed**; JavaScript syntax and diff checks pass.
  `python scripts/redesign_smoke.py --stage shell` passed desktop 1440×1000 and
  mobile 390×844: navigation/history/reload, text/link/PDF/image/audio/drop/paste,
  real MediaRecorder with fake device, stop/review/cancel, failed draft preservation,
  automatic organization and no horizontal overflow. No model calls on reads.
- Known slice 1 boundary: process-local deferred extraction is not a durable job
  queue. Originals survive interruption; interrupted processing needs retry. File
  drafts stay in memory; text drafts are session-scoped per owner. The old reports were
  replaced by slice 2. URL fetch/transcription are smoke fixtures.
- Exact next step: review PR #23 against `main` and check its fresh CI status.
  PR #18 and PR #20 are merged; the Inbox dependency landed through PR #22.
  The next product improvement is the labeled obligation/
  retrieval-quality evaluation in the follow-up document.
  Do not repeat discovery or start an unrequested extra UI workstream.
- PR integration: merge in order after review. For each child, retarget to main
  after its parent merges, update branch to remove already-merged commits, rerun CI.
  No merges are authorized in this implementation session.
