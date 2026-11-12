# Architecture

## Stack

- **API:** FastAPI
- **ORM:** SQLAlchemy
- **Migrations:** Alembic (authoritative schema owner)
- **DB:** SQLite locally (temp/test DBs for development)
- **UI:** HTML + vanilla JS/CSS
- **AI:** OpenAI optional; deterministic fallback organizer always available

## Core modules

| Module | Responsibility |
| --- | --- |
| `app/main.py` | HTTP routes, error shaping |
| `app/database.py` | Models, persistence helpers, fingerprints, task upsert |
| `app/schemas.py` | Request/response and analysis contracts |
| `app/organizer.py` | OpenAI + fallback organization |
| `app/analysis/` | Incremental input/version fingerprints, bounded supporting context, strict proposals and orchestration |
| `app/capture/` | Rich capture (URL, PDF, image, voice, lifecycle) |
| `app/retrieval/` | Keyword + local TF-IDF search, grounded Ask My Inbox |
| `app/attention/` | Timing parse, attention buckets, briefing helpers |
| `app/integrations/` | Replaceable Calendar adapter (mock / preview-only Google) |
| `app/auth.py` / `app/config.py` | Session auth + startup config validation |
| `app/storage_backend.py` | Upload storage abstraction (local FS default) |
| `app/templates` / `app/static` | Browser UI |

## Startup and configuration

FastAPI's lifespan in `app/main.py` validates settings before calling `init_db()`
or accepting requests. `app/config.py` allows the built-in session secret only in
`local`; beta/production require a private secret of at least 32 characters,
PostgreSQL, and complete Google credentials when Google auth is selected.
Unknown environment names are rejected. Authentication helpers also enforce
nonlocal validation rather than bypassing it. Invalid session signatures, claims,
expired sessions and unknown users are treated as unauthenticated requests.

Alembic remains the schema owner; startup does not run migrations. Health reports
configuration metadata, not database connectivity or full deployment readiness.

## Data model (current)

- `inbox_items` — canonical captures
- `notes` — legacy safety copy (not authoritative)
- `topics` — user-editable spaces/projects (seeded defaults)
- `analysis_runs` — analyze attempts; new runs persist only proposed/applied deltas and metrics
- `item_analysis_state` — per-capture input/version fingerprints, claims, invalidation generations and recovery state
- `task_details` / `analysis_reviews` — sourced enrichment and reviewable uncertain matches
- `item_insights` — AI category/title/priority/tags/rationale/topic plus user overrides
- `item_relations` — duplicate / near-duplicate / related pairs (dismissible)
- `tasks` / `task_source_items` — durable tasks; multiple tasks per source allowed;
  AI vs confirmed timing distinguished

## Analysis freshness

`POST /api/analyze` claims at most eight new, changed or explicitly invalidated
captures. A persistent input/contract/model fingerprint prevents unchanged targets
from being re-analyzed, including after restart. With no eligible targets it
bypasses the organizer entirely. Bounded metadata/keyword context supplies existing
tasks and source snippets separately from targets. Validated proposed changes are
applied transactionally; unrelated tasks and organization remain stable.

`GET /api/analyze/latest` projects the current durable records. New-run freshness
uses pending eligible inputs, not changes to presentation fields. The legacy
whole-inbox JSON format/fingerprint can still be read. See
[`incremental-analysis.md`](incremental-analysis.md) for budgets, invalidation,
lookups, ownership validation, migration and recovery behavior.

## Schema evolution

1. Change SQLAlchemy models
2. Add an Alembic revision under `migrations/versions/`
3. `alembic upgrade head` on empty and previous-milestone DBs in the quality gate

`LIFE_INBOX_AUTO_CREATE_TABLES=1` is allowed only for isolated automated tests.

## Rich capture lifecycle

URL, PDF, image, and voice captures follow: **capture → save original →
process → extract → organize → ready/failed**.

- Original sources are persisted before extraction (`source_url` or `storage_path`).
- `processing_status`: `captured`, `processing`, `ready`, `failed`.
- `POST /api/items/{id}/retry` re-runs processing for failed capture items.
- Images: OCR text in `extracted_text` (editable facts); AI copy in
  `ai_description` (inferred). Thumbnails via `?variant=thumb`.
- Voice: original audio retained; transcript in `extracted_text` (editable);
  browser MediaRecorder upload supported when the mic is available.
- Ready captures feed incremental targets using effective extracted/raw source text.

Uploads default to `data/uploads/` (override with `LIFE_INBOX_UPLOAD_DIR`).
OCR/vision/transcription adapters degrade gracefully when not configured
(`LIFE_INBOX_DISABLE_OCR=1`, `LIFE_INBOX_DISABLE_TRANSCRIPTION=1` for tests).

## Retrieval + Ask My Inbox

- `GET /api/search?q=` — ranked passages with keyword and semantic scores
- `POST /api/ask` — extractive grounded answers with per-item citations
- Insufficient evidence returns an explicit “not enough information” response (no fabrication)
- Semantic layer is local TF-IDF cosine similarity (no vector database, no paid API in tests)

## Attention & safe actions

- Captures save timing reference timestamps/timezones; substantive user source
  edits get new references. Task generation tracks source versions separately
  from a person's deadline choice.
- Timing phrases stay on tasks (`timing_text` / `ai_timing_text`); saved date-only
  `due_on`, provenance and review state power Attention and calendar previews.
  Reads never reparse relative wording. See `docs/deadlines.md` for edit, legacy,
  timezone, uncertainty and task-reuse rules.
- Ambiguous timing sets `needs_confirmation` until the user confirms.
- Snooze / reminder fields are local only (`snoozed_until`, `reminder_at`).
- Calendar: `GET /api/calendar/status`, `POST /api/calendar/preview`,
  `POST /api/calendar/confirm`. Mock confirm stores a local receipt.
  Google provider is preview-only; confirm is blocked (no OAuth / no writes).

## External services

Automated tests mock OpenAI and must not call paid APIs. Live calls are for
explicit manual smoke testing only. Never enable real Calendar OAuth/writes
without owner approval.

## Browser application

The active UI uses `app/static/app.js` for shared navigation/requests and date
editing, `composer.js` for unified capture and bounded automatic organization,
`inbox.js` for the canonical collection/item dialog, `tasks.js` for saved task
views/details/review, and `ask.js` for grounded answers and owner-scoped recovery.
There is no frontend build step. Browsing never queues model work. Query parameters
identify destinations, filters and open details; native dialogs preserve context.
See `redesign-checklist.md` for the four PRs, exact verification and remaining limits.
