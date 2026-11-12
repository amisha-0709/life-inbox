# Incremental organization contract

`POST /api/analyze` organizes one bounded delta. Previously processed, unchanged
captures are supporting evidence at most, never implicit analysis targets. The
browser button is **Organize new / changed items**. The same
`app/analysis/service.py::run_incremental_analysis` service can be invoked by a
worker with an explicitly bound owner; this change does not install a scheduler.

## Persistent eligibility and invalidation

`item_analysis_state` stores a successful input fingerprint, analysis version,
invalidation/processed generations, attempt fingerprint, claim, error and run ID.
The SHA-256 input in `app/analysis/fingerprints.py` includes the stable item ID,
type, effective source text (`extracted_text` when present, otherwise `raw_text`),
source URL, recorded timing reference and timezone. The version is `delta-v1`
plus `OPENAI_MODEL`. Restart the process after changing model configuration;
bump the contract version when changing extraction/prompt semantics deliberately.

Only ready captures with effective text are eligible. New inputs, changed input
fingerprints, a changed contract/model version, or an explicit invalidation
generation queue work. Reads, reloads, generated titles/descriptions, ordinary
`updated_at` changes, filtering/navigation and task choices do not invalidate.
Adding/removing an API key alone does not queue an already processed capture.
To deliberately retry under a different provider, queue that capture explicitly.

`POST /api/items/{id}/invalidate-analysis` queues one owned capture; the item card
exposes **Queue re-analysis**. It does not clear prior results. Successful writes
acknowledge only the claimed fingerprint and generation. A concurrent source edit,
invalidation or referenced context correction rejects the transaction, leaving
the user's saved data intact. Atomic claims prevent concurrent requests from
both analyzing the same input. An interrupted claim becomes failed after 15
minutes when the next organization run checks it; it requires explicit retry.

Failures and budget blocks persist across restarts. They are not retried on every
ordinary organization call. Change the source or use **Queue retry** in
**Organization review**, then organize again. `/api/analysis/status` exposes this
state. An empty delta bypasses the planner entirely: exactly zero organizer-model
calls and zero consumed organizer input tokens. Capture transcription and Ask
are separate services, not included in these counts.

## Bounded inputs and targeted context

| Input | First pass | One optional targeted lookup |
| --- | --- | --- |
| Targets | At most 8, 24,000 characters per item, 48,000 total | Same targets |
| Existing tasks | At most 6 | At most 8 total |
| Existing source snippets | At most 6 | At most 8 total |
| Supporting context JSON | At most 8,000 characters including metadata | At most 10,000 total |
| Organizer-model calls | At most 1 | At most 1 additional call |

Larger inbox deltas remain pending for the next run. Oversized individual sources
are blocked, never silently truncated as targets. Split or shorten the source
before retrying. Context descriptions are clipped to 240 characters, source
snippets/source wording to 600, with truncation markers. At most 12 provenance
links are included per context task, retaining target links first and reporting
omissions. Authoritative corrections are retained; a record that cannot fit is
omitted rather than silently editing its correction fields.

`app/analysis/context.py` ranks owner-scoped candidates using source links,
typed references and keyword overlap. Context includes stable IDs, concise text,
dates/status, timing provenance, user choices, sourced details and snapshot
fingerprints. Targets and read-only context are separately labeled in the prompt.
No full inbox, full task list, prior global analysis JSON or generated UI fields
are sent to the organizer. Candidate selection currently scans the owner's local
records; bounded model context does not imply indexed database retrieval.

The organizer can request at most two concise queries/ID lists in one round.
A query naming an invoice/reference/order/booking/ticket retrieves that exact
typed reference, not every invoice through generic keyword overlap. A completed
negative lookup is distinguished from an omitted or clipped result. The second
pass retains omission evidence. Still-insufficient context yields a 409 and
blocked targets without applying provisional results; it never expands to the
entire inbox. Metadata/keyword matching is the starting point; semantic retrieval
or reranking needs representative relevance measurements first.

## Proposed changes and reconciliation

`app/analysis/contracts.py` rejects extra fields, unknown IDs and invalid local
references. Output contains organization only for the targets and named changes:

- `create_task`: new obligation from explicit target source IDs.
- `attach_source`: provenance for a clearly equivalent saved task/occurrence, or
  a task created earlier in this batch using its proposal ID.
- `update_task`: add a bounded, separately stored, sourced detail. It cannot
  rewrite a saved deadline, status, priority, text or user correction.
- `flag_possible_duplicate` / `flag_related`: retain separate identities and
  create an owner-scoped review instead of guessing.

Database reconciliation rechecks owner, target membership, supplied context IDs,
current source/task snapshots and claim generation before committing anything.
It independently checks equivalence before automatic attachment. Existing task
IDs, completion/dismissal, explicit confirmed/edited/cleared deadlines and source
links survive. Supporting items' organization is not refreshed. A changed source
may update its own matched obligation through the existing conservative
[deadline edit policy](deadlines.md); unchanged temporal intent keeps its old
date/reference. One saved occurrence cannot absorb two new timing clauses.

Exact wording requires a known equal occurrence date and capture day before
automatic attachment. A shared typed stable reference, equal occurrence date,
same action and non-conflicting numeric details can also establish equivalence.
Reusable account IDs, similar wording, unrelated sibling clauses and different
reference namespaces cannot establish it. Recurring captures stay separate unless
they carry an explicit shared occurrence identity; the current capture UI does
not assign such identities. Completed undated tasks never absorb new occurrences.

Additional provenance/detail never reinterprets the saved deadline. Ambiguous
matches remain visible in **Organization review** with **Keep separate** or
**Link source; keep tasks**. The latter deliberately links provenance without
deleting either task or resolving conflicting choices. A safe interactive merge
of two existing tasks is not implemented. Reviews are deduplicated per source
fingerprint/candidate/kind; a material new edit can require a fresh review.

`analysis_runs.result_json` stores the target IDs/fingerprints, validated plan,
applied changes and metrics, not a regenerated global task list. Responses and
`GET /api/analyze/latest` project the current durable records for the existing UI.
Legacy whole-inbox analysis helpers remain for compatibility; the HTTP analysis
route does not call them.

## Migration and verification

Alembic revision `0015_incremental_analysis` adds processing state, sourced
details and reviews. It does not reinterpret or rewrite existing captures/tasks.
Legacy successful items bootstrap as processed only when their stored task-source
fingerprint still matches their current source and their owned insight points to
a successful run. Unknown or changed legacy inputs are eligible while existing
tasks/corrections remain intact. Migration and smoke checks use disposable SQLite
databases. A personal database is never a development fixture.

Run `env -u OPENAI_API_KEY .venv/bin/python -m pytest -q --tb=short` for the suite,
or select `tests/test_analysis_context.py`, `tests/test_analysis_planner.py`,
`tests/test_incremental_analysis.py`, `tests/test_deadline_edits.py`,
`tests/test_isolation.py` and `tests/test_migrations.py` for the contract checks.

Each successful response/run reports target IDs/count/characters, supporting task
and snippet counts, serialized context characters, model-call attempts, lookup
count and remaining pending count. `input_tokens` is provider-reported measured
usage, or NULL when unavailable (including an unreported failed request).
`estimated_input_tokens` uses prompt characters / 4, rounded up, only for calls
actually attempted; it is not a tokenizer measurement. With no calls both values
are exactly zero. `serialized_input_characters` measures the batch JSON across
attempted calls; the token estimate additionally includes instructions/schema.
Automated provider tests mock reported usage; they are not live cost measurements.
