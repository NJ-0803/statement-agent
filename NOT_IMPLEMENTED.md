# NOT_IMPLEMENTED.md

Everything knowingly left out of this build, consolidated in one place, each with the actual reasoning
— not a vague "future work" list. Per the brief's own instruction ("note anything unfinished
honestly"), this is written with the same rigor as `EDGE_CASES.md`'s gap entries. §A–§E are the 8
unresolved gaps from that audit, regrouped by root cause; §F and §G were surfaced later, by checking
this build against an external capability checklist rather than the edge-case catalogue; §H was
surfaced later still, by manually red-teaming the verifier itself and by a design discussion about
what breaks at real (years-long) scale rather than this dataset's 3-month sample.

None of what follows caused a wrong number to reach a user silently — that was checked, not assumed
(see `EDGE_CASES.md`'s summary). What follows is capability that doesn't exist yet, disclosed rather
than hidden behind a confident-looking feature list.

---

## A. Cross-transaction linking (the single biggest gap — one root cause, many symptoms)

**What's missing:** an `EconomicEvent` layer that links *related* transactions to each other — a
refund to its original purchase, a reimbursement to the expense it covers, an EMI's monthly principal
to the purchase it's paying off, a foreign-currency charge to its INR settlement and FX fee.

**What this actually breaks, concretely** (these are EC-04, EC-17, EC-28, and the "linking" half of
EC-01/EC-02/EC-03/EC-18/EC-48 from `EDGE_CASES.md`):
- A ₹10,000 purchase followed by a ₹3,000 partial refund reports as ₹10,000 gross spend (correct) but
  never as ₹7,000 *net* spend, because nothing connects the refund row to the purchase row it applies to.
- A pending and a later-posted version of the same charge would both count, since there's no state
  machine tracking "this is the same economic event, just updated."
- A foreign purchase, its INR settlement, and a separate FX markup fee would be treated as three
  independent transactions rather than one linked event with a financing cost.

**Why it's not built:** this is a genuinely hard problem — matching a refund to *the correct* original
purchase (not just any purchase of the same amount) needs real matching logic (merchant, amount,
time-window, reference number) that itself needs real fixture data with actual refund/reimbursement/
EMI pairs to build and verify against. None of that pattern exists in `dataset_public/`. Shipping an
unverified matcher would be worse than not having one — a wrong match ("this refund pays off THAT
purchase" when it doesn't) actively corrupts a net-spend figure, which is precisely the "confident but
wrong" failure mode this whole project is built to avoid. This is the correct thing to build next if
the project continues past the 3-day box, and it's the one piece of future work worth calling a
priority rather than a nice-to-have.

**Live evidence this matters, not just theoretical:** the 95-question red-team eval bank (`DECISIONS.md`
§19) asked *"Which reimbursement claims can you match to card transactions?"* — the agent reached the
correct answer, but only after 23 individual tool calls doing brute-force pairwise search, close to
`MAX_TOOL_ITERATIONS`. Right answer, wrong cost — real confirmation that this gap would bite on a larger
ledger even though it didn't produce a wrong answer here.

---

## B. Extraction robustness gaps with no real fixture to build against

**EC-06 — OCR digit errors.** If a scanned page misreads ₹8,100 as ₹3,100, nothing catches it. The
one mechanical defense that could catch this — reconciling extracted transactions against a
statement's own stated total — is implemented and unit-tested (`resolve.reconcile_document`), but
**no document in this dataset states an opening/closing balance**, so there's no total to reconcile
against here, and no realistic way to fabricate a convincing test fixture for "OCR got a specific
digit wrong" without it being an arbitrary, ungrounded guess at what OCR failure modes look like.

**EC-12 — running balance mistaken for the transaction amount.** The amount-anchor regex in
`pdf_native.py` takes the *last* numeric token on a line as the amount. If a statement had a running
balance column after the actual amount (`Amazon 500.00 24,735.62`), the balance would be picked up
instead. No statement in this dataset has a balance column — this is a real, confirmed structural risk
in the parser, not a hypothetical one, but fixing it without a real multi-number-per-line fixture to
verify against risks trading a known-safe behavior for an unverified one.

**EC-42 — European number formatting.** `1.234,50` (period as thousands separator, comma as decimal)
is confirmed, by direct test, to misparse as `1.234`. Not fixed: a correct fix needs genuine
document-locale detection — the catalogue's own guidance is "interpret only when document locale
provides sufficient evidence; otherwise flag ambiguity," which is a real feature (infer locale from
surrounding numbers, currency, or document metadata), not a one-line regex change. Given it doesn't
occur anywhere in `dataset_public/` (every amount here is Indian-grouped or plain Western format),
this was judged lower-priority than the fixes that were made, and disclosed rather than rushed.

---

## C. Schema gaps

**EC-10 — no separate `posted_date`.** `Transaction` has one date field. A purchase made March 31 but
posted April 2 would only ever be tracked under whichever date extraction happened to capture — there's
no explicit "which date drives month/quarter calculations" policy because there's only one date to
choose from. Adding the field without a documented, tested query policy for which date wins in a
period calculation would be a half-built feature — the schema change is cheap, the policy and its test
coverage are not, and none of this dataset's statements actually distinguish the two dates to build
that policy against.

**EC-32 — no canonical account ID.** Different documents are never merged into one logical account
(e.g. "HDFC XX1234" and "HDFC Savings" as the same account under different labels). The safe default —
never wrongly merging two different accounts — is what the system does today by treating every
document independently; true alias resolution needs a stable identifier (account number, a
consistently-formatted label) that isn't reliably present across this dataset's documents to build a
mapping from.

---

## D. Categorization sophistication

**No LLM-assisted fallback for unrecognized merchants.** `resolve.categorize()` is a binary
keyword-match cascade: a merchant either matches a known keyword list or gets `category: None`
(`UNKNOWN`), with no soft-confidence middle ground. This is deliberately conservative — the brief
values "I don't know" over a guess, and an unmatched merchant staying `UNKNOWN` is exactly that. What's
missing is the *next* tier both this build and the competing plan described: merchant rule → keyword
rule → LLM classification → unknown. Adding it would also let the Answer Stability range reflect
genuine category-confidence uncertainty, not just the duplicate/date-implausibility uncertainty it
currently tracks. Not built because it adds LLM calls (and non-determinism) to something the test
suite currently checks with an exact expected output — worth doing with a real budget for building and
evaluating the categorizer's accuracy properly, not as a quick addition.

**Merchant-alias normalization — partially built.** `normalize.normalize_merchant()` (wired into
`resolve.assign_merchant_normalization`, run before duplicate detection) now strips noise that's
unambiguous regardless of which specific merchant it is — whitespace, case, and a trailing
corporate-entity suffix (PVT, PVT LTD, LTD, LIMITED, INC, LLC, CO) that different banks/processors
inconsistently append for the exact same company. `merchant_normalized` is a new field alongside
`merchant_raw` (never replacing it — citations still use the raw value), and both
`detect_cross_document_duplicates` and `aggregate_spending`'s `group_by="merchant"` now group on it, so
"GRANDEUR JEWELLERS PVT" on one statement and a hypothetical "GRANDEUR JEWELLERS" on another would
correctly consolidate. See `DECISIONS.md` §20.

**Deliberately still deferred: a curated brand-alias dictionary** mapping genuinely different-looking
strings for the same merchant (`AMZN` / `Amazon Marketplace` / `AMZN Mktp IN`, or resolving which side of
a payment-processor's `PREFIX *SUFFIX` pattern is the real merchant). "AMZN" was added as a one-off
keyword to `categorize()`'s Shopping list after the edge-case audit found it missing, but generalizing
that into a real alias table needs actual collision fixture data to build and verify against — this
dataset's own merchant strings are already internally consistent (no two spellings collide for the same
real merchant), so there's nothing to verify a curated table against yet, and guessing at asterisk-split
direction risks actively corrupting grouping rather than just failing to help it — the same reasoning as
§A's cross-transaction-linking deferral.

---

## E. Testing and observability depth

**No property-based test suite.** The current 152 tests are all example-based (specific input, specific
expected output). A Hypothesis-based suite could check invariants that should hold regardless of input
specifics — "duplicating a source document never changes the verified total," "reordering transaction
rows never changes an aggregate answer," "adding an internal transfer never changes total spend." These
are strong, cheap-to-state properties; not built for time, not because they're not valuable.

**Model reasoning is now captured — built.** Previously true, no longer: `--trace` used to print only
tool name + tool input, with no separate "why it chose this tool" text captured anywhere, even in
memory. `ToolCallRecord` now carries a `reasoning` field (the model's own text alongside each tool call,
extracted from the response's text blocks in `agent/loop.py`'s `run_agent`), and `AgentRunResult` carries
a top-level `final_reasoning` for the text alongside the winning `final_answer` call — the "why," not
just the "what," is available on the same trace object correctness already relies on. `cli.py --trace`
prints both. This uses the model's ordinary response text, not Claude's separate extended-thinking
feature — deliberately: `verify()`'s grounding/citation checks only ever inspect `tool_result`, never
`reasoning`, specifically so a model "reasoning" its way to a wrong number still fails verification on
the actual grounded check (tested in `tests/test_loop.py`). See `DECISIONS.md` §20.

**Still not built: a persisted, cross-session execution-trace log.** The reasoning/trace data now exists
in full on every `AgentRunResult`, but nothing writes it to durable storage — `--trace` and the web UI
both show it only for the current run; a grader can inspect what the agent did on the question they just
asked, but there's no log file to review after the session ends. Cheap to add now that the data itself
is captured (append each `AgentRunResult` to a JSONL file, one line per question) — not done because it's
a pure storage/retrieval addition with no correctness impact on its own.

---

## F. Currency conversion — built (no longer a gap)

Was the top item here; now implemented (`statement_agent/fx.py`, wired into `aggregate_spending` via
`convert_to`). See `DECISIONS.md` §17 for the full writeup — architecture, why a bundled open-data
file was used instead of a live API call, and live-verified results. Left as a placeholder entry here
rather than deleted outright, since the *reasoning for why it mattered* (flagged directly against this
build's real context: a card issuer's customers routinely have foreign-currency transactions even
without leaving India) is worth keeping visible next to the rest of this audit.

---

## G. Context/memory management as the ledger grows

This section was originally a single open gap. Two parts of it are now **built** (see `DECISIONS.md`
§18 for the full writeup); the rest remains deliberately deferred, for the same reason as the rest of
this document — no large real ledger exists yet to build and verify the remaining pieces against
safely.

**Built — #1: default row cap + truncation disclosure on `search_transactions`/`get_sources`.**
Previously, `search_transactions` had a `limit` *parameter* but no default — an unfiltered or
loosely-filtered call against a hypothetical 100,000-row ledger would try to serialize thousands of
`TxnView` rows into one tool result, and `get_sources` (looking up a caller-supplied ID list) had no
cap at all. Both now return a `SearchResult` (`results`, `total_matched`, `truncated`, `limit_applied`)
capped at `DEFAULT_SEARCH_LIMIT = 200` when the caller doesn't pass an explicit `limit`. The cap itself
is not the interesting part — silently returning a partial list that *looks* complete is exactly the
EC-26 incomplete-retrieval failure mode this project is built to avoid, so `truncated`/`total_matched`
are always populated and the system prompt (rule 6b) and tool descriptions require the agent to
disclose a truncated result rather than presenting it as the full match set. `aggregate_spending` and
`compare_periods` were already safe at any scale (they return numbers, not raw rows) and needed no
change.

**Built — #2: `detect_cross_document_duplicates` was O(n²), now O(n).** Not originally listed in this
document at all — found during the same review that produced #1. The original implementation compared
every candidate transaction against every other candidate directly; invisible at ~90 real transactions
(a few thousand comparisons) but infeasible at real scale (100,000 transactions → 5 billion
comparisons). Rewritten to group candidates by `(amount, currency, merchant)` first (O(n)), so the
expensive pairwise date-tolerance comparison only ever runs within one small group of transactions that
already match on everything else — matching semantics are unchanged, only the algorithm is. Verified
against a synthetic 20,000-transaction ledger with 25 planted duplicates: all found, zero false
positives, completes in well under a second.

**Still deferred — #3, #4, #5, in the order they'd matter:**

- **#3 — push filtering into SQL.** `search_transactions` currently loads the *entire* ledger into
  Python and filters in-memory; the cap in #1 protects the tool *result*, not the work done to produce
  it. At real scale the filtering itself (category/date/merchant matching over 100,000+ rows on every
  call) should move into the SQL query in `store.py` instead of `list[Transaction]` comprehensions.
  This is the natural next step once #1's cap proves the shape is right, but it changes the read path
  more invasively than #1 did, so it's deferred rather than built alongside it.
- **#4 — paginate `list_documents`.** Same class of gap as `search_transactions` had, but for document
  count rather than transaction count. Not urgent — this dataset has 7 source documents, and even a
  heavy real user is unlikely to have thousands of *statements* (as opposed to transactions) — but the
  same truncation-disclosure pattern from #1 would apply directly if it ever needed it.
- **#5 — pre-computed rollups for very large scale.** At a scale where even SQL-side filtering (#3) is
  too slow per-question (e.g. millions of transactions), the next step would be maintaining
  pre-aggregated summaries (by month/category/merchant) at ingest time, so `aggregate_spending` reads a
  rollup instead of scanning raw rows. This is meaningfully more complex than #1-#4 (it introduces a
  second source of truth that must stay consistent with the raw ledger, including after dedup flags
  change) and isn't worth building without a real dataset at that scale to validate the consistency
  story against.

**Also relevant, noticed while fixing #1 but not in scope for it:** `aggregate_spending`'s
`verified_transaction_ids`/`uncertain_transaction_ids`/`conversion_details` fields grow with the number
of *matched* transactions, unbounded — the same class of risk #1 fixed for `search_transactions`, just
not addressed here since it changes `AggregateResult`'s contract (dropping or sampling ID lists) rather
than being a purely additive cap, and the right fix is naturally connected to #3: once filtering moves
into SQL, an aggregate's full ID list stops making sense to return at all, and citing sources should
go through a follow-up `search_transactions` (with the same filter) + `get_sources` call instead, which
inherits the cap built here for free.

---

## H. The verifier's remaining blind spot, and where confidence would erode at real scale

**Built, not a gap anymore: ungrounded decimal claims in prose.** `verify()` used to only check the
structured `verified_amounts`/`cited_transaction_ids` fields — a manual red-team test found that
`answer_text`/`caveats` were never scanned at all, so a plausible-but-fabricated number in free prose
(a statistical threshold, in the actual case found) passed verification by coincidence, not by guarantee.
Fixed — see `DECISIONS.md` §22 for the full incident, including a bug in the fix itself caught before
shipping (the grounding walk didn't originally extract numbers embedded inside a longer string like a
`notes` field, so it would have flagged genuinely-sourced numbers as fabricated).

**Still a real gap: prose claims about the conversation itself.** The Q27 pattern (§19/§20) —
misattributing a detail to "what the user said" that they never said — has no structural check behind it,
only the prompt-level rule 3a. A decimal-grounding check can't catch this: there's no number to
cross-reference against tool output, since the false claim is about the *conversation's own content*, not
the ledger. A real fix would need the actual original question text passed into `verify()` and a
narrower, phrase-triggered check (e.g. a claim following "as you said"/"you confirmed" cross-referenced
against whether the referenced detail literally appears in the user's own message) — buildable, but a
genuinely different, fuzzier piece of work than the decimal check, not a natural extension of it. Not
attempted yet; scoped out explicitly rather than bolted on half-working.

**Where scale would erode confidence, discussed before most of it was built (`DECISIONS.md` §23–§24).**
Concrete risks identified for a corpus of years, not months — ranked by how dangerous each actually is,
and updated as two of them got built on explicit direction:

- **`dataset_coverage` gap detection — built.** Was Tier 1 (silent false confidence): min/max bounds alone
  made a missing quarter of statements look like full coverage. Now reports `coverage_gaps` — contiguous
  calendar-month ranges with zero transactions inside the ledger's own date range — and a new prompt rule
  (4e) requires disclosing an overlapping gap as a floor. See `DECISIONS.md` §24.
- **Ingestion throughput — built.** Multi-page vision-OCR extraction now runs concurrently (bounded at 4
  workers) with retry/backoff on transient failures only, instead of a sequential loop with no retry at
  all. Honestly caveated: this dataset has no document with 2+ pages needing vision, so the concurrent
  *dispatch* path has never been exercised at real multi-page volume — only the retry logic itself is
  thoroughly tested offline. See `DECISIONS.md` §24.
- **`detect_anomalies`'s baseline — still open, and the risk was mischaracterized before checking the code.**
  Not actually one global baseline blended across the whole ledger's history, as first described — it's
  scoped *per document* already (`resolve_all` runs once per statement). The real problem is closer to the
  opposite: a light-activity statement produces an unstable baseline from a tiny sample, and there's no
  cross-document view of a person's broader spending pattern, unlike duplicate detection which already has
  one (`detect_cross_document_duplicates`). The right fix mirrors that exact existing pattern — a
  `detect_cross_document_anomalies` pass over the whole ledger, windowed (calendar-month buckets merged
  until a minimum sample size is met, to avoid reintroducing the O(n²) risk a naive continuous sliding
  window would have) rather than either today's per-document scope or a true global blend. Not built —
  this changes computed z-scores on the *current* dataset too, not just future data, and needs verification
  against the existing Grandeur Jewellers/Croma/GoIndigo outlier flags before shipping; deferred on
  explicit direction rather than rushed.
- Duplicate-detection tolerance windows were shaped for this dataset's clean synthetic timing, not
  validated against real multi-year billing-cycle noise (Tier 1, still open).
- Category-keyword drift, the reimbursement gap, and FX-file staleness all degrade gracefully — already
  honestly disclosed via existing caveat mechanisms, just increasingly less useful over time (Tier 2).
- `MAX_TOOL_ITERATIONS=12` would block a genuinely complex multi-year trend question — fails safe (no
  answer) rather than silently wrong (Tier 3).

---

## Summary: what would change if this continued

**§F (currency conversion) is done** — see `DECISIONS.md` §17. **§G's #1 (default cap + truncation
disclosure) and #2 (O(n²) → O(n) dedup) are done** — see `DECISIONS.md` §18. Of what's left:

- **If optimizing for architectural completeness:** §A (the `EconomicEvent` linking layer). It's the
  root cause behind the largest number of individually-small gaps, and — unlike §B and §C — there's a
  clear, buildable path to it that just needs real fixture data with actual refund/EMI/reimbursement
  pairs to build and verify against safely.
- **If optimizing for scale beyond this dataset:** §G's #3-#5 — pushing `search_transactions`'s
  filtering into SQL, paginating `list_documents`, and eventually pre-computed rollups. None are urgent
  at ~90 transactions; #3 is the natural next step whenever a real large ledger exists to validate
  against.
- **If optimizing for trust at real (multi-year) scale specifically:** §H's `dataset_coverage` gap
  detection — cheapest of the newly-identified risks, and the only Tier-1 (silent false-confidence) one
  with an obvious, bounded fix rather than a genuinely hard problem (`detect_anomalies`'s stale-baseline
  risk and the duplicate-tolerance mistuning both need real multi-year data to validate a fix against,
  the same constraint that's deferred §A and §D this whole time).

---

## Tool ideas floated, not committed

Asked directly what other tools would extend the trust story rather than just add surface area — none
of these are scoped or scheduled, just recorded so they don't get re-derived from scratch later:

- **Merchant-alias normalization** as a standalone tool/pass — generalizes the ad-hoc "AMZN" keyword
  fix from §D into something systematic.
- **Recurring-charge / subscription detector** — distinct from duplicate detection; "this looks like a
  monthly pattern" is a different signal than "this looks like the same charge twice."
- **Budget/limit tracking tool** — "how much of my ₹X monthly dining budget is left" — needs a budget
  concept that doesn't exist yet, so this implies its own small feature, not just a tool.
- **Export/report tool** — hand the user back a clean statement or CSV of what the agent found, usable
  outside the chat itself.
- **"Explain this transaction" tool** — given one transaction ID, return its full context (nearby
  transactions, why it was or wasn't flagged, its category reasoning) for a natural "why did you say
  that" follow-up.
- **A standalone single-amount conversion tool** (`convert_amount(120, "USD", "INR", date)` on its
  own, separate from `aggregate_spending`'s `convert_to`) — for a one-off "how much is $120 in INR"
  question that isn't about a transaction total. `fx.convert_amount` already exists and is tested;
  this would just be a thin tool-schema wrapper around it, not new logic.
