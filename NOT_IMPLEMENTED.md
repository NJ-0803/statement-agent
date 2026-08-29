# NOT_IMPLEMENTED.md

Everything knowingly left out of this build, consolidated in one place, each with the actual reasoning
— not a vague "future work" list. Per the brief's own instruction ("note anything unfinished
honestly"), this is written with the same rigor as `EDGE_CASES.md`'s gap entries, because that's
exactly what most of these are: the 8 unresolved gaps from that audit, regrouped here by root cause
and joined by a few things that predate that audit.

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

**No general merchant-alias fuzzy matching.** "AMZN" was added as a specific keyword after the
edge-case audit found it missing, but there's no general normalization layer mapping merchant name
variants (`AMZN` / `Amazon Marketplace` / `AMZN Mktp IN`) to one canonical merchant while preserving
the raw description. Not needed for this dataset's merchant names, which are already consistent per
merchant; would matter at real-world scale with messier real bank feeds.

---

## E. Testing and observability depth

**No property-based test suite.** The current 152 tests are all example-based (specific input, specific
expected output). A Hypothesis-based suite could check invariants that should hold regardless of input
specifics — "duplicating a source document never changes the verified total," "reordering transaction
rows never changes an aggregate answer," "adding an internal transfer never changes total spend." These
are strong, cheap-to-state properties; not built for time, not because they're not valuable.

**No persisted execution-trace log.** `--trace` on the CLI prints the tool-call sequence for the
current run, and the web UI shows it per-answer, but neither is saved anywhere — a grader can inspect
what the agent did on the question they just asked, but there's no log to review after the session
ends. Cheap to add (the `ToolCallRecord` data already exists in memory); not done because it's a pure
observability nicety with no correctness impact, and time went to the fixes that changed actual
answers first.

---

## Summary: what would change if this continued

If picking exactly one item to build next: **A (the `EconomicEvent` linking layer)**. It's the root
cause behind the largest number of individually-small gaps, and — unlike B and C — there's a clear,
buildable path to it (merchant + amount + time-window matching, the same technique both this build and
the source plans already describe) that just needs real fixture data with actual refund/EMI/
reimbursement pairs to build and verify against safely.
