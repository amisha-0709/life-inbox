# Redesign evidence and handoff

The requirement map and exact resume instructions are in
[redesign-checklist.md](redesign-checklist.md). This document records interaction
evidence rather than another work plan. All screenshots use synthetic captures
and a disposable migrated database, never a personal inbox.

| Everyday job | Before | After |
| --- | --- | --- |
| Capture and find, desktop | [Oversized header, separate forms and repeated reports](screenshots/redesign/before-desktop-inbox.png) | [One composer and compact collection](screenshots/redesign/inbox-desktop.png) |
| Capture and find, mobile | [Capture forms push the collection below the screen](screenshots/redesign/before-mobile-inbox.png) | [Compact navigation and capture](screenshots/redesign/inbox-mobile.png) |
| Complete tasks, desktop | [Briefing/Attention and repeated task information](screenshots/redesign/before-desktop-tasks.png) | [One saved-task collection](screenshots/redesign/tasks-desktop.png) |
| Complete tasks, mobile | [Large repeated task panels](screenshots/redesign/before-mobile-tasks.png) | [Completion, dates and focused views](screenshots/redesign/tasks-mobile.png) |
| Ask, desktop | [Provider banner and large header](screenshots/redesign/before-desktop-ask.png) | [Readable answer and source actions](screenshots/redesign/ask-desktop.png) |
| Ask, mobile | [Question crowded by surrounding UI](screenshots/redesign/before-mobile-ask.png) | [Question, concise answer and inspectable sources](screenshots/redesign/ask-mobile.png) |

Details preserve context rather than opening a competing collection:
[desktop item](screenshots/redesign/inbox-detail-desktop.png),
[mobile item](screenshots/redesign/inbox-detail-mobile.png),
[task/date editing](screenshots/redesign/task-detail-desktop.png),
[focused review](screenshots/redesign/review-mobile.png),
[recording review](screenshots/redesign/voice-review-mobile.png), and
[Ask source inspection](screenshots/redesign/ask-source-mobile.png).

Capture now has one Save action and keeps failed inputs. Attachment removal and
recording start/stop/review/cancel are explicit. The original is saved before
background extraction; only capture, source edits or explicit retries queue
incremental organization. Browsing never queues models.

Inbox has one paginated collection. Corrections are distinct from editing the
original, survive analysis, and drive search and filters. Details use a desktop
side dialog and the mobile screen; Back/Forward, reload, focus, filters and list
position were exercised. Tasks use saved facts, completion/undo, separate date
editing and source inspection; 40-row loading reaches all records. Review is
available when something needs a decision. Ask citations open the same details;
owner-scoped cached answers restore without requests and show refresh guidance.

## Executed checks

These checks were rerun after incorporating `main` at `3d9ed1a` (merged PR #20)
into PR #23. The full walkthrough retained the request counts below and produced
no browser exceptions; fresh Ask and mobile source screenshots were reviewed.

- `env -u OPENAI_API_KEY .venv/bin/python -m pytest -q --tb=short --show-capture=no`:
  **486 passed**. Includes empty/legacy migrations, ownership, concurrency,
  incremental input/result contracts, corrections and deadline invariants.
- `node --check` for the active browser modules and smoke script; `git diff --check`:
  passed.
- `python scripts/redesign_smoke.py --stage all --full`: real Chrome desktop
  **1440×1000** and mobile **390×844**, initially **167 captures / 164 tasks**.
  Full interaction checks and actual local account switching passed with no browser
  exceptions. [Machine-readable result](screenshots/redesign/browser-results.json).
- A final focused mobile run additionally exercised dismiss/undo/restore and
  checked that additional task IDs belonged to genuinely new source captures.
  Fresh Ask checks verified the final deduplicated extractive answer formatting.
- Full-run measured HTTP POST counts were **31 analysis / 9 Ask** across explicit
  capture, correction and question scenarios. These are HTTP requests, not model
  calls. With no provider key, actual provider calls and consumed input tokens
  were **zero**. Navigation, citations, history, reload and owner recovery added
  zero Ask/organizer requests.

Keyboard checks cover labels, modal entry/wrapping, Escape/return focus and normal
form/checkbox controls. Shared controls have visible focus and at least 44px touch
areas; layouts were checked for horizontal overflow and visually inspected.
This is browser emulation and code/visual review, not a physical-device or
screen-reader user study.

## Remaining boundaries

URL retrieval and transcription are explicit local fixtures; recording uses real
MediaRecorder with Chrome's fake microphone. Live providers, physical microphone
hardware, PostgreSQL concurrency and unbounded collection scale remain unverified.
Extraction is process-local and interrupted work retains originals for retry.
File drafts remain in memory. Task metadata is fetched as an owned collection and
paged in the browser. Individual topic clearing retains the existing API limit;
Reset all corrections is explicit. Calendar confirmation stays a local mock.

Reference material can still become an unwanted obligation, and retrieval can
match irrelevant common words. Concrete reproductions and the next evaluation
scope are in [extraction-quality-followup.md](extraction-quality-followup.md).
Answer formatting deduplicates identical passage wording while retaining source
provenance; it does not claim to resolve semantic relevance or classify intent.

PRs [#18](https://github.com/tmiiitd/life-inbox/pull/18) and
[#20](https://github.com/tmiiitd/life-inbox/pull/20) are merged; the Inbox changes
from #19 landed through [#22](https://github.com/tmiiitd/life-inbox/pull/22).
Review [#23](https://github.com/tmiiitd/life-inbox/pull/23) against `main` with fresh
CI after conflict resolution. Migrations 0016 and 0017 belong to slices 2 and 3;
this final slice adds no schema migration. Conflict-resolution work does not
merge PR #23, deploy or migrate a personal database.
