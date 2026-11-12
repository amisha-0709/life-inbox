# Follow-up: reference material becoming tasks

The redesign keeps historical tasks and exposes their source, dismissal and
duplicate review. It does not delete or merge tasks to make the collection look
cleaner. These extraction problems need a separate, measured change.

## Reproduced causes

The screenshots show report/document headings and captured page controls mixed
with personal obligations. Inspection and pure-function checks found:

| Representative input | Current result | Cause |
| --- | --- | --- |
| `Task planning and tool use: how agents work` | Creates a task | `app/organizer.py::_split_action_tasks` checks substrings; `ask` matches inside `Task`. |
| `Follow us on Facebook.` | Creates a task | The same substring test matches `book` inside `Facebook`. |
| `Founders should research customer needs and prepare a prototype.` | Creates a committed task | The fallback recognizes action words without determining whether the captured author is addressing this user. Report advice is treated as an obligation. |
| An article with `<nav>Ask a question</nav>` and `<footer>Book a demo</footer>` | Extracted text includes both controls; both can create tasks | `app/capture/url_fetch.py::_HTMLMetaParser` excludes scripts/styles but retains navigation, footer, buttons and other page furniture. |

These examples were executed against `_split_action_tasks` and `extract_article`
without accessing a personal database or calling a provider. They demonstrate
causes; they are not a representative quality benchmark. PDF extraction in
`app/capture/pdf_extract.py::extract_pdf_text` keeps page text, including headings
and recommendations, without an obligation/reference distinction.

The active incremental path calls `fallback_analyze` from
`app/analysis/planner.py::fallback_delta`; it therefore inherits the same behavior.
The incremental model prompt separates targets/context and preserves dates and
IDs, but does not explicitly require evidence that a document action is the
user's own obligation. Model output and fallback output should both be evaluated.
Similar repeated obligations are a separate matching problem; the incremental
contract intentionally retains uncertain matches for review and keeps recurring
occurrences distinct.

## Focused next scope

1. Add a small labeled fixture set covering direct commitments, quoted advice,
   report conclusions, headings, navigation controls, reference documents and
   user-written instructions attached to a document. Include true obligations
   without dates and multiple genuine actions from one note.
2. Fix word-boundary false positives and article-body extraction while preserving
   the original URL/file and extracted text provenance. Do not broadly drop all
   document actions: an assignment or invoice can contain a real user obligation.
3. Require evidence of user obligation before creating a committed task. Represent
   suggested actions as reviewable suggestions when ownership/intent is unclear,
   with a deliberate accept action. Define this contract for model and fallback.
4. Measure false positives and missed obligations on the fixtures before choosing
   further classifiers, prompt changes or semantic matching. Keep incremental
   input budgets, task identity, user corrections, date references and isolation
   tests green.
5. Make reprocessing an explicit, scoped operation. Do not silently invalidate an
   entire personal inbox or reinterpret historical dates when this policy changes.

Acceptance should include: the first four false positives above create no
committed obligations; `I need to pay the electricity bill tomorrow` still creates
a task with the recorded capture deadline; genuine document obligations remain
discoverable; historical completion/corrections remain unchanged. No live-model
quality claim is made by the interface redesign.

## Ask relevance observation during integration

In the populated synthetic workshop/dentist fixture, “What is the moonbase launch
password ZXQ2147?” returned unrelated saved passages with an extractive answer.
Common query words can produce a positive retrieval score despite the requested
fact being absent. The interface correctly exposes the supplied evidence but
cannot establish that this evidence answers the question. This is a retrieval
quality issue, not evidence of semantic accuracy from a successful browser test.

Track a separate relevance evaluation covering unsupported entities/facts,
stopwords, near-topic distractors and genuinely answerable questions before
changing scoring or confidence thresholds. The UI insufficient-evidence state is
verified using a query with no matching evidence; that does not resolve the
false-positive example above. No live-model quality claim is made.
