# NOT_IMPLEMENTED.md

Everything knowingly left out of this build, consolidated in one place, each with the actual reasoning
— not a vague "future work" list. Per the brief's own instruction ("note anything unfinished
honestly"), this is written with the same rigor as `EDGE_CASES.md`'s gap entries. §A–§E are the 8
unresolved gaps from that audit, regrouped by root cause; §F and §G were surfaced later, by checking
this build against an external capability checklist rather than the edge-case catalogue.

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

**No persisted execution-trace log, and no captured model reasoning.** `--trace` on the CLI prints the
tool-call sequence for the current run, and the web UI shows it per-answer, but neither is saved
anywhere — a grader can inspect what the agent did on the question they just asked, but there's no log
to review after the session ends. What's captured is also narrower than "comprehensive": tool name +
tool input, not tool *output*, and — since this build never requests Claude's extended-thinking blocks
— there's no separate "why it chose this tool" reasoning text to capture even if the trace were
persisted; the only artifacts are the tool-call sequence and the final `answer_text`. Cheap to add (the
`ToolCallRecord` data already exists in memory, and thinking blocks are a request-time flag away);
not done because it's a pure observability addition with no correctness impact on its own, and time
went to the fixes that changed actual answers first.

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

**What's missing:** any strategy for the ledger outgrowing what fits comfortably in the agent's
context. Right now the *entire* ledger is loaded into memory and handed to `run_agent()` on every
question (`cli.py`/`web/app.py` both call `store.all_transactions()` in full), and `search_transactions`
has no default row cap — an unfiltered or loosely-filtered call against a hypothetical 100,000-row
ledger would try to serialize thousands of `TxnView` rows into one tool result. `aggregate_spending`
and `compare_periods` are safe at any scale (they return numbers, not raw rows), but `search_transactions`
and `get_sources` aren't currently protected against this.

**Why it's not built:** this dataset is ~90 transactions; the problem genuinely doesn't manifest here,
and building a chunking/pagination/summarization strategy without a large real ledger to verify it
against risks the same "unverified fix" trap as the extraction gaps in §B. The right first step is
cheap and low-risk though — unlike most of §B, this doesn't need new matching logic, just bounds on
what's already built: a sane default `limit` on `search_transactions` (already has the parameter,
just no default cap), and eventually pagination on `list_documents` once document count, not just
transaction count, gets large. Worth treating as a near-term fix rather than a deferred one, since
unlike the linking layer it doesn't need fixture data to build safely — it only needs a sensible
default value.

---

## Summary: what would change if this continued

**§F (currency conversion) is done** — see `DECISIONS.md` §17. Of what's left:

- **If optimizing for architectural completeness:** §A (the `EconomicEvent` linking layer). It's the
  root cause behind the largest number of individually-small gaps, and — unlike §B and §C — there's a
  clear, buildable path to it that just needs real fixture data with actual refund/EMI/reimbursement
  pairs to build and verify against safely.
- **If optimizing for cheap, near-term downside protection:** §G (context safety at scale) — a default
  `limit` on `search_transactions`/`get_sources`, not new logic. Worth doing regardless of what else
  gets picked, since it's pure protection with no design risk.

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
