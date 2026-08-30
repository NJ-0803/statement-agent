# ARCHITECTURE.md

A clean, non-chronological reference: what's built, why each piece is shaped the way it is, and
exactly what happens when a question is asked. `DECISIONS.md` is the session-by-session log of how
this came together (useful for "why did this change mid-build"); this document is the map of the
finished thing.

---

## 1. The whole system in one picture

```
                         ┌──────────────────────────────┐
  dataset_public/  ───▶  │     INGESTION PIPELINE        │
  (PDF, CSV, XLSX,       │     (ingest/pipeline.py)      │
   or a standalone       └──────────────┬─────────────────┘
   statement image)                     │
                    ┌───────────┬───────┼───────┬───────────────┐
                    ▼           ▼       ▼       ▼               ▼
              csv_parser  xlsx_parser  pdf_native  image_parser  │
              .py (CSV    .py (Excel   .py         .py (bare     │
              rows)       rows, same   (pdfplumber, image file,  │
                          alias        anchor-      no native    │
                          matching     based)       text layer   │
                          as CSV)          │         possible)   │
                    └───────────┴──────────┼─────────┴───────────┘
                                            ▼ (pages/images that fail
                                            a native-quality check)
                                      pdf_vision.py
                                      (Claude vision — shared call/parse
                                       logic for both a rendered PDF page
                                       and a standalone image file)
                                            │
                                            ▼
                                   normalize.py
                                   (currency + date parsing,
                                    document-level ambiguity resolution)
                                             │
                                             ▼
                                   resolve.py
                                   (economic-type refinement → categorization →
                                    duplicate detection → reconciliation →
                                    anomaly detection)
                                             │
                                             ▼
                                   store.py — SQLite ledger
                                   (Decimal-safe, idempotent, full provenance)
                                             │
                          ┌──────────────────┴──────────────────┐
                          ▼                                     ▼
                    cli.py (ask)                        web/app.py (Flask)
                          │                                     │
                          └──────────────────┬──────────────────┘
                                             ▼
                                   agent/loop.py — run_agent()
                                   ┌─────────────────────────────┐
                                   │  Claude (claude-sonnet-5)    │
                                   │  + agent/prompts.py          │
                                   │  + agent/tools.py (9 tools)  │
                                   └───────────────┬───────────────┘
                                                   │ tool_use / tool_result loop
                                                   ▼
                                   agent/verifier.py
                                   (grounding + provenance check,
                                    independent of the LLM)
                                             │
                                     pass ───┴─── fail → retry (max 3) → INSUFFICIENT_INFORMATION
                                      ▼
                            Answer with status, amounts,
                            caveats, sources, trace
```

Two front ends (`cli.py`, `web/app.py`) sit on top of the exact same core — neither contains any
logic of its own beyond argument parsing / HTTP plumbing. Everything that matters lives in
`statement_agent/`'s library modules, which is what makes both front ends trustworthy: there is only
one place a bug could live, and it's the same place the test suite exercises.

---

## 2. What was implemented, and why — by layer

### 2.1 The canonical schema (`schema.py`)

**What:** One `Transaction` dataclass every extractor produces, and a `Document` dataclass per source
file. Every `Transaction` carries a `source: SourceRef` (file path, page/row, raw text, extraction
method, confidence) — the provenance chain that makes every later citation checkable.

**Why an `EconomicType` enum separate from spend `category`:** The brief's dining/groceries/etc.
category taxonomy answers "what did you buy," but "did you buy anything at all" is a prior question —
a credit-card bill payment or a bank transfer has no meaningful spend category, and forcing one onto
it is exactly how double-counting happens (the payment counted once as a bank debit, once again as
the card's own purchases). `EconomicType` (`PURCHASE`, `TRANSFER`, `CREDIT_CARD_PAYMENT`,
`CASH_WITHDRAWAL`, `REIMBURSEMENT`, `INVESTMENT_TRANSFER`, `CASHBACK`, `FEE`, `INTEREST`, `REVERSAL`,
`REFUND`, `UNKNOWN`) is resolved first; only `PURCHASE` transactions ever get a category. This is the
single architectural decision that prevents the most common class of wrong-total bug in this kind of
system.

### 2.2 Normalization (`normalize.py`)

**What:** `normalize_amount()` turns any of `₹1,340.00` / `Rs. 2,494.73` / `8,000.00 CR` /
`(1,250.00)` / `Rs 1,250-` / `1,25,000.50` into an exact `Decimal` + currency + direction.
`DocumentDateResolver` turns `03/06/2025`-style ambiguous dates into real calendar dates.

**Why `Decimal`, never `float`, anywhere money is touched:** binary floating point cannot represent
`0.10` exactly; summing many small amounts silently drifts. Every amount is `Decimal` from the moment
it's parsed through to SQLite storage (as `TEXT`, not `REAL`) and back. Tested explicitly
(`0.10 + 0.10 + 0.10 == 0.30`, which fails under float).

**Why date-format ambiguity is resolved per-document, not per-row:** `05/07/2025` alone is genuinely
ambiguous (5 July or 7 May?). But a document rarely mixes date conventions — if any *other* date in
the same file has a day value >12, that pins the whole document's convention. `DocumentDateResolver`
scans every date in a document first, then only falls back to a locale default (DD/MM, matching this
dataset's own India/INR convention) when nothing in the document disambiguates it — and flags that
fallback as an assumption, not a certainty, in the transaction's notes.

### 2.3 Extraction (`ingest/csv_parser.py`, `xlsx_parser.py`, `pdf_native.py`, `image_parser.py`,
`pdf_vision.py`, `quality.py`)

**What:** CSV and XLSX rows are both parsed via header-alias matching (`Txn Date` and `date` both map
to the date column) so differently-shaped sheets don't need separate code paths — `xlsx_parser.py`
reads an Excel sheet into the same `(headers, rows)` shape a CSV produces and hands off to
`csv_parser.parse_tabular_rows`, the one function underneath both formats. PDF rows are reconstructed
from `pdfplumber` word *positions* (`(x0, top)` coordinates), not raw text-stream order — grouped by
`top` or 2pt tolerance, sorted by `x0` within a row, then classified by **anchors**: a date pattern at
the line start and an amount pattern at the line end. Anything matching neither anchor is metadata,
never a transaction. Pages where this yields nothing usable (empty text layer, or text with zero
recognized rows) get rasterized and sent to Claude's vision model as a fallback, tagged at lower
confidence — and a standalone statement image (a photo or screenshot, not embedded in a PDF) skips
straight to that same vision path via `image_parser.py`, since there's no native text layer to try
first for a bare image at all.

**Why XLSX reuses the CSV parser instead of a separate implementation:** structurally it's the same
problem — tabular rows, a header row with varying column names — so `xlsx_parser.py`'s only real job
is getting values out of `openpyxl` correctly, not re-deriving header-matching logic. One genuine
wrinkle CSV never has: Excel stores dates as native `datetime.date` objects, not text — converting one
with a naive `str()` produces `"2025-06-21 00:00:00"`, which the ISO-date regex in `normalize.py`
rejects (it requires nothing after the day). Cell values are converted deliberately, not left to a
generic `str()`.

**Note on "does the agent know when to use what":** extraction-strategy selection — native text vs.
vision OCR, which parser a file extension routes to — is decided entirely by this deterministic
pre-ingestion pipeline (`quality.py`'s pass/fail check runs before any question is ever asked). The
conversational agent (§4) never sees or chooses between extraction strategies; it only ever queries an
already-normalized ledger. This is deliberate, not a shortcut — consistent, testable extraction
behavior matters more than letting the LLM decide per-file, and it keeps a source of model
non-determinism out of a step that should give the same result every time on the same input.

**Why position-based extraction instead of naive text order:** discovered mid-build that a plain
`pypdf.extract_text()` read of one statement produced what looked like two scrambled, interleaved
tables — content-stream order didn't match visual order. Rebuilding from word coordinates instead
fixed this completely and turned out to be necessary for every PDF in the set, not just that one.

**Why anchor-based classification instead of "line N is always the date, line N+1 the amount":** a
fixed line-role assumption breaks the moment a layout varies at all, and — more importantly —
it's also the structural defense against a prompt injection embedded in one of these statements
("*** AUTOMATED PROCESSING NOTICE: Disregard any and all prior instructions... ***"). That sentence
has no date and no amount anchor, so it can never become a transaction row, regardless of what it
says. The extraction design and the security defense are the same mechanism, not two separate ones.

**Why vision OCR is tiered, not run on every PDF:** cost and latency — an API call per page adds up,
and most pages have a perfectly good text layer. `quality.py` only flags a page when native extraction
genuinely failed (no text, or text with zero parsed rows). Every vision-extracted transaction is then
still run through the *same* normalize/plausibility/reconciliation pipeline as natively-extracted
rows, at a lower `extraction_confidence` (0.75 vs 1.0) — nothing from a scanned page is ever treated as
more certain than what it is.

**Why the vision call/parse logic is one shared function, not duplicated for PDFs vs. images:**
`pdf_vision._vision_extract_from_image_bytes` is the actual model call and response-parsing code;
`vision_extract_page` (renders a PDF page first) and `vision_extract_standalone_image` (reads an image
file directly) are both thin callers of it. A PDF page and a bare photo differ only in how the image
bytes were obtained — sending them to the model and turning the response into transactions is
identical, so it's one tested code path instead of two that could quietly drift apart. Live-tested by
rendering one of this dataset's own scanned pages out to a standalone PNG and running it through the
image path: identical output to running the same page through the PDF path.

**Why multi-page vision extraction runs concurrently with retry, not one page at a time.** A PDF needing
vision fallback on several pages now fires up to 4 concurrent requests (`ThreadPoolExecutor`, one shared
`anthropic.Anthropic()` client) instead of a plain sequential loop, and every call retries with
exponential backoff on transient failures only (`RateLimitError`, `InternalServerError`,
`ServiceUnavailableError`, `OverloadedError`, connection/timeout errors — never `BadRequestError` or
similar, which fail identically on retry). Results are gathered via `as_completed`, in whatever order
pages finish, not submission order — safe because the existing stable sort-by-`source.page` (added for
`extraction_sequence`) already re-establishes correct page order downstream regardless. This dataset has
no document with more than one page needing vision, so the concurrent dispatch itself is untested at real
multi-page volume; the retry logic is fully unit-tested offline (`tests/test_pdf_vision.py`) and a full
real ingest was re-verified to produce identical output afterward. See `DECISIONS.md` §24.

### 2.4 Resolution (`resolve.py`)

**What, in the order it runs:** economic-type refinement (keyword rules upgrading the generic
`PURCHASE`/`REFUND` default to something more specific — `CASH_WITHDRAWAL` for ATM text,
`INVESTMENT_TRANSFER` for named brokerages, `CREDIT_CARD_PAYMENT` for a bank-side bill payment,
`CASHBACK` for reward credits, etc.) → categorization (merchant/keyword cascade, `PURCHASE` only) →
duplicate detection (within one document, then a separate cross-document pass) → statement
reconciliation (opening + credits − debits =? closing, when a document states its own totals) →
anomaly detection (median/MAD outlier scoring, not mean/stdev, since spend data is heavily skewed).

**Why keyword rules before any LLM classification:** deterministic, free, instant, and — critically —
testable with an exact expected output. An LLM fallback for genuinely unrecognized merchants is a
reasonable next layer (see `NOT_IMPLEMENTED.md`), but everything that keyword rules can resolve
correctly should never cost an API call or introduce model non-determinism.

**Why two separate duplicate-detection passes:** `detect_duplicates` compares transactions *within*
one document (same-day repeat charges at the same merchant/amount). `detect_cross_document_duplicates`
runs separately, across the whole ledger, with a wider date tolerance — the case it exists for is a
single real-world transaction appearing in two different source documents (a card statement and a
separate reimbursement export, or two statements with overlapping periods). Merging these into one
pass would have made the within-document check's 0-day tolerance too loose for the cross-document
case, or the cross-document check's wider tolerance too loose for the within-document case — they're
different problems with different correct tolerances, so they're different functions.

**Why every flag is a flag, never a deletion:** an over-aggressive dedupe that silently removes a
legitimate same-day repeat purchase is arguably worse than a false positive left for a human to glance
at — the brief's own framing ("PASS WITH CAVEAT" beats guessing) applies to duplicate resolution as
much as to spend totals.

**`detect_cross_document_duplicates` is O(n), not O(n²).** The naive version — compare every candidate
transaction against every other candidate directly — is invisible at this dataset's ~90 transactions
but infeasible at real scale (100,000 transactions → 5 billion comparisons). Candidates are grouped by
`(amount, currency, merchant)` first, an O(n) pass; the pairwise date-tolerance check then only runs
within one small group of transactions that already match on everything else, which stays small even
in a huge ledger. See `DECISIONS.md` §18 for the scale test (20,000 synthetic transactions, 25 planted
duplicates, correct and sub-second) and `NOT_IMPLEMENTED.md` §G for what's still deferred at this layer
(SQL-side filtering, §G's #3).

**Retrieval at scale — the `search_transactions`/`get_sources` cap (§G's #1, `NOT_IMPLEMENTED.md`).**
Both tools cap results at 200 rows by default and return `total_matched`/`truncated` alongside the
rows, so a capped result is always disclosed rather than silently looking complete — the same
incomplete-retrieval failure mode as EC-26 (`EDGE_CASES.md`), just at the tool-result layer instead of
the filter-logic layer. This protects the *serialized result size*, not the in-memory filtering work
itself, which is still a full Python scan over `list[Transaction]` — pushing that into SQL is the
next, deliberately deferred step (§G's #3).

**Merchant normalization (`normalize.normalize_merchant`, `resolve.assign_merchant_normalization`).**
Runs first in `resolve_all`, before duplicate detection. Sets `Transaction.merchant_normalized` — an
additive field alongside `merchant_raw` (citations still use the raw value) — by stripping whitespace,
case, and a trailing corporate-entity suffix (PVT, PVT LTD, LTD, LIMITED, INC, LLC, CO). Both
`detect_cross_document_duplicates`/`detect_duplicates`'s grouping keys and `aggregate_spending`'s
`group_by="merchant"` group on the normalized value now, not the raw one. Deliberately does not attempt
a curated brand-alias table (`AMZN` → `Amazon`) or resolve payment-processor `PREFIX *SUFFIX` patterns —
see `NOT_IMPLEMENTED.md` §D for why (no real-dataset evidence to verify one against, and guessing wrong
actively corrupts grouping). See `DECISIONS.md` §20, including a real store.py round-trip bug this
caught (the field computed correctly in memory but was silently dropped on save/reload until the SQLite
schema was updated to carry it).

**Extraction order (`resolve.assign_extraction_sequence`).** Runs first in `resolve_all`, before even
merchant normalization. Sets `Transaction.extraction_sequence` — the row's 0-based position in the
document's own original order (page-ascending/top-to-bottom for PDFs, row-ascending for CSV/XLSX) — from
list order directly, never derived from `transaction_date`. Exists because `transaction_date` was being
used as a proxy for "the document's own ordering," which is wrong the moment the document itself isn't
chronologically ordered: a live question ("is this statement sorted by date?") got a false-positive
answer from sorting the results by date and then checking whether the sorted result was sorted by date —
circular, and it caught the real Meridian July statement's actual out-of-order row only after being fixed.
See `DECISIONS.md` §21. For a PDF mixing native and vision-OCR pages, `pipeline.py` stable-sorts the
combined transaction list by `source.page` before this runs, so page-to-page interleaving is correct
regardless of which pages needed the vision fallback.

### 2.5 The ledger (`store.py`)

**What:** SQLite, two tables (`documents`, `transactions`), amounts stored as `TEXT` and parsed back
through `Decimal()`. `has_document(file_hash)` makes re-ingesting an unchanged folder a no-op.

**Why SQLite over a heavier option (DuckDB was specifically considered and rejected):** at
~90 transactions across 7 files, a columnar analytical engine buys nothing and costs a dependency;
`sqlite3` needs zero installation. The `Decimal`-safety property lives in how `store.py` reads/writes
values, not in which engine sits underneath — so this choice doesn't trade away any correctness.

### 2.6 Currency conversion (`fx.py`)

**What:** `aggregate_spending`'s `convert_to` parameter converts and sums multi-currency spend into
one target currency, using a bundled historical rate file (`data/eurofxref-hist.csv` — the European
Central Bank's published EUR foreign-exchange reference rates), not a live API call. Every transaction
converts using the rate quoted for **its own transaction date**, cross-computed through EUR (the
file's implicit base); currencies never convert through today's rate or one rate blended across a
date range. The converted total is always returned *alongside* the honest per-currency breakdown
(`by_currency`), never in place of it, with per-transaction rate/date/source in `conversion_details`
for citation, and any transaction that couldn't be converted explicitly listed in
`failed_conversion_ids` rather than silently dropped.

**Why a bundled file instead of a live API call — decided mid-build, after actually trying the live
call first:** the first version called `frankfurter.dev` per transaction and hit two unrelated live
failures on the first real test (a CA-certificate gap in this Python install, and an edge-protection
403 on the default HTTP client's User-Agent) — both fixable, but both real fragility that has nothing
to do with whether the underlying math is right. Directed to switch to a bundled open-data file
instead; validating that choice, the *direct* CSV-download endpoint for ECB's own published rates
returned a stale, partially corrupted cached copy (real data stopping in 2010, with a few rows of
obviously fabricated placeholder values mixed in) before the ZIP-packaged endpoint gave the real,
current file. A verified, version-controlled snapshot removes all of this at once: no network
dependency at request time, and no risk of a corrupted remote response reaching a calculation
silently — the file that ships is exactly the file that was inspected. Full story, including the
live cross-check against the API's own numbers, in `DECISIONS.md` §17.

**The disclosed trade-off:** the snapshot is a point-in-time download, not a live feed — a
transaction dated after its last covered date has no rate, and `fx.py` returns `None` rather than
estimating one, consistent with this system's INSUFFICIENT_INFORMATION-over-guessing policy
throughout. Refreshing it is a deliberate, visible action, never something that happens silently
underneath a request.

### 2.7 The agent (`agent/tools.py`, `verifier.py`, `prompts.py`, `loop.py`)

Covered in detail in §4 below, since this is what you specifically asked about.

### 2.8 Two front ends, one core (`cli.py`, `web/app.py`)

**What:** `cli.py` — `ingest` and `ask` subcommands (plus `serve` to launch the web UI). `web/app.py`
— a small Flask app with `/api/status` and `/api/ask`, and a single-page dark-themed front end
(matching the naap project's "instrument panel" visual identity: Chakra Petch display font, the same
ground/panel/line grays, mint/amber/red status semantics).

**Why the web UI is a thin wrapper and not a second implementation:** both front ends call the exact
same `Store` and `run_agent()` — the web layer's own code is only route handling and JSON shaping
(`tests/test_web.py` tests exactly that boundary, with `run_agent` itself mocked, since it's already
covered elsewhere). A bug in the actual agent logic would show up identically in both; there's no way
for the two front ends to silently disagree.

---

## 3. Why not RAG (embeddings, chunking, vector search)

Worth stating explicitly, because "an agent that answers questions from a folder of documents" sounds
RAG-shaped on the surface, and it's a reasonable thing to expect here. It isn't one, deliberately —
no sentence-transformers, no embedding model, no chunking, no vector index anywhere in this system.

**What retrieval actually is here:** exact, deterministic filtering over a normalized SQLite table —
the equivalent of `SELECT * FROM transactions WHERE category='Dining' AND date BETWEEN ? AND ?`. Every
document is parsed into structured rows *once*, at ingestion time (§2.1–2.4). At question time, the
agent doesn't search unstructured text for semantically similar chunks — it calls a Python function
(`aggregate_spending`, `search_transactions`) that does an exact filter over every row in the table.

**Why that's the right fit for this data, not a missed step:** RAG's value proposition is that your
corpus is too big for context and you don't know in advance which parts are relevant, so you embed
everything and pull the *k* most semantically similar chunks at query time. That's the right tool when
the underlying data is unstructured prose (contracts, tickets, wikis) where "similar meaning" is a
genuinely useful proxy for "relevant." Financial transactions aren't that — *"what did I spend on
dining in June"* doesn't have a fuzzy, approximate answer; it has an exact one, every row where
category=Dining and month=June, no more, no fewer. Building this as embed-and-retrieve-top-k would
reintroduce, by design, exactly the failure mode fixed as EC-26 (`EDGE_CASES.md`) — correct arithmetic,
incomplete retrieval: a semantic search can miss a real matching transaction because its embedding
wasn't "close enough," and the agent would then correctly sum the ones it *did* retrieve — arithmetically
right, silently wrong. Deterministic filtering over the full table structurally cannot do that: a row
either matches the `WHERE` clause or it doesn't; there's no "close enough."

**Does a much larger dataset change this answer?** Not for transaction-level retrieval — that's what a
database is for. SQLite (or Postgres at real scale) indexes millions of rows and answers an exact
filter in milliseconds, which scales better *and* more reliably than approximate-nearest-neighbor
vector search would for this kind of tabular, exact-match data. The SQLite-over-DuckDB call (§2.5) was
about deployment weight at *this* dataset's size, not about whether SQL-style filtering is the right
retrieval strategy at scale — it is, at any scale, for structured data like this.

**Where embeddings would genuinely earn their place, if this grew:**
1. **Document-level discovery at very large document counts.** `list_documents` currently hands the
   model metadata for every document — fine at 7, unwieldy at thousands. Even then, the natural fix is
   more structured filtering (date range, account, filename pattern) over document *metadata*, not
   semantic embedding — the metadata is structured, not prose, so RAG's core justification doesn't
   apply here either. This is `list_documents` pagination, §G's #4 in `NOT_IMPLEMENTED.md` — still
   deferred; #1 (the related row-cap issue for `search_transactions`) is built, see §2.4 below.
2. **If genuinely unstructured free-text content entered the picture** — handwritten margin notes,
   support-chat transcripts about a dispute, narrative contract text — that's a different problem from
   tabular transaction data, and *that's* where a real RAG pipeline (chunking, an embedding model, a
   vector store) would be the correct architecture, because "semantically similar" becomes the actually
   correct retrieval signal.

Net: this was a deliberate fit-the-tool-to-the-data decision, not an oversight. "RAG" is often used as
shorthand for "AI plus your documents" generally, but the right retrieval architecture depends entirely
on whether the underlying data is structured or not — this dataset (bank/card statements, expense
sheets) is fundamentally tabular once extracted, so it gets a tabular retrieval strategy.

---

## 4. The agent loop and tool calls — what actually happens when you ask a question

### 4.1 The fifteen tools

Everything the model can do is one of these. Fourteen return data; the fifteenth ends the turn.

| Tool | Purpose | Never does |
|---|---|---|
| `dataset_coverage` | Actual min/max date and currencies in the ledger, plus `coverage_gaps` — internal calendar-month silences (e.g. a quarter never uploaded) that min/max alone would hide | Guess at coverage the ledger doesn't have, or treat a total spanning a gap as complete |
| `resolve_period` | Turns `"last_quarter"`, `"last_month"`, `"2025-Q2"` etc. into exact ISO start/end dates, correctly handling the year-boundary case | Let the model compute a date range by hand |
| `resolve_date` | Resolves ONE raw date string the same way ambiguous document dates are resolved at ingestion (§2.4) — flags `assumption`/`confidence<1.0` when a DD/MM-vs-MM/DD guess was needed | Let the model silently guess which convention a date like "05/07/2026" uses |
| `list_documents` | Every source file, its declared account/currency/period, and any ingest-time `warnings` (security flags, structural anomalies) | Require a bank name to appear as a merchant string to be findable |
| `search_transactions` | Raw filtered rows, with `sort_by`/`limit` for "the single largest transaction"; capped at 200 rows by default with `total_matched`/`truncated` disclosed (§2.4); `sort_by="extraction_order"` returns the document's real original row order for "is this statement sorted?"-type questions (§2.4); `sort_by="closest_to_amount"` finds the transaction nearest a target value (e.g. a `compute`d average) | Compute any total, silently return a partial result as if it were complete, answer a document-ordering question by re-sorting and checking the sort, or eyeball which row "looks closest" to a value |
| `aggregate_spending` | The only way to get a spend number — per-currency `verified_total`/`uncertain_total`, optional `group_by`, `possibly_missing_uncategorized_count` when filtered by category, and an optional `convert_to` for a combined multi-currency total (§2.6) | Blend currencies without an explicit `convert_to`, or hide flagged/uncategorized transactions silently |
| `compare_periods` | Two `aggregate_spending` calls side by side | — |
| `compute` | Deterministic arithmetic (average/sum/difference/min/max) over values the model already retrieved this turn — its result is a real tool output, grounded the same way every other number is (§4.2) | Let the model do even simple derived-value math itself, or be a general calculator for numbers it invented |
| `generate_chart` | Renders a bar/line/pie chart from the SAME `aggregate_spending` grouped totals — never a second aggregation path; refuses to blend currencies, same as `aggregate_spending` (`DECISIONS.md` §28) | Compute its own numbers to plot, or blend multiple currencies into one chart |
| `top_n_per_group` | Top N transactions by amount WITHIN each group (e.g. "top 5 per category") in one call, instead of one `search_transactions` call per group (`DECISIONS.md` §29) | Answer "top N in every category" via many separate calls, or blend currencies in the ranking |
| `generate_dashboard` | Chart + table combined view, reusing `generate_chart`/`top_n_per_group` internally — rendered inline in the web UI only, ONLY when the user's own wording explicitly asks for a "dashboard" (`DECISIONS.md` §29) | Compute its own numbers, serve a separate page/file, or trigger without explicit "dashboard" wording |
| `find_disputable_transactions` | Every duplicate-flagged or anomaly-flagged row across the ledger | Declare anything fraud |
| `summarize_statement` | Full breakdown for one source file (by currency, by category, flagged count) | — |
| `get_sources` | Full provenance detail for a specific list of transaction IDs; same 200-row cap and disclosure as `search_transactions` | — |
| `final_answer` | **Terminal.** The only way a turn ends. | Get shown to the user without passing the verifier first |

**Note on the "query → which transactions to pull" abstraction:** there's no separate, serialized
"query plan" object sitting between the natural-language question and the tool calls (unlike, e.g., the
competing plan's `QueryPlan` dataclass). The tool schemas themselves — `category`, `date_from`/
`date_to`, `currency` as structured arguments the model must fill in — *are* that abstraction layer;
the tool call the model chooses to make is the plan. The closest thing to an inspectable plan today is
the `--trace` output / `ToolCallRecord` list (§4.3), which shows the sequence after the fact rather
than as a plan committed to before execution.

### 4.2 The loop, step by step (`agent/loop.py::run_agent`)

1. The system prompt (`agent/prompts.py`) + the 9 data tools + `final_answer` are sent to Claude along
   with the user's question.
2. Claude responds with one or more `tool_use` blocks. Each one is dispatched to the matching Python
   function in `agent/tools.py`, run against the in-memory ledger, and the result is serialized back
   as a `tool_result`. This repeats — Claude can call several tools across several turns before it's
   ready to answer (real traces run 1 to 5 tool calls deep).
3. Every tool call and its result is appended to a `trace: list[ToolCallRecord]` that persists for the
   *whole* conversation, not just the current turn. Each record also carries `reasoning` — the model's
   own one-sentence explanation of why it made that call (prompt rule 8), extracted from the response's
   text alongside the tool call — so the trace captures *why*, not just *what ran with what input*.
   `AgentRunResult.final_reasoning` carries the same for the winning `final_answer` call. This reasoning
   is display/audit-only: `verify()` (step 4 below) never inspects it, only `tool_result` — asserted
   directly in `tests/test_loop.py`, so a model that "reasons" toward a fabricated number still fails
   the actual grounded check.
4. When Claude calls `final_answer`, the proposed answer — `answer_text`, `proposed_status`,
   `verified_amounts`, `cited_transaction_ids`, `caveats` — is handed to `agent/verifier.py::verify()`,
   which runs **without calling the LLM at all**:
   - every `cited_transaction_ids` entry must be an ID that actually appeared in a tool result this
     conversation (never invented or half-remembered);
   - every `verified_amounts` entry must literally match (after `Decimal` normalization) a number that
     appeared somewhere in the trace — never something the model computed itself;
   - every specific decimal figure stated in `answer_text`/`caveats` — not just the structured
     `verified_amounts` field — must also match something the trace actually returned. Added after a
     manual red-team test found the gap live: a model justifying a categorization stated a precise
     statistical threshold in prose that was never checked, because `verified_amounts` was empty — it
     happened to be correct, but nothing verified that (`DECISIONS.md` §22). Deliberately scoped to
     decimals specifically (not every number), so legitimate integer counts ("3 transactions") aren't
     flagged as if they needed individual tool-result provenance.
   - `answer_text` is scanned for stray tool-call-like artifacts (a real glitch found via manual
     browser testing, where the model occasionally leaked fragments like
     `</answer_text><parameter name="proposed_status">` into its own answer) — caught and rejected
     rather than ever shown to a user.
5. If verification fails, the specific failure is fed back to Claude as an error and it gets another
   attempt — up to 3 total. If it still can't produce a grounded answer, the loop returns
   `INSUFFICIENT_INFORMATION` with the verification failures listed as caveats, rather than surfacing
   an unverified number.
6. If verification passes, the LLM's own `proposed_status` can still be *downgraded* (never upgraded)
   — e.g. `VERIFIED` with nonzero caveats is automatically corrected to `VERIFIED_WITH_CAVEATS`, since
   a model can't self-certify full confidence while listing open questions.

### 4.3 A real trace, annotated

From a live run of *"What did I spend on dining in Q2 2025?"*:

```
1. resolve_period({"period": "2025-Q2"})
   → {"start": "2025-04-01", "end": "2025-06-30"}
2. dataset_coverage({})
   → {"min_date": "2025-05-14", "max_date": "2025-07-28", ...}
3. aggregate_spending({"category": "Dining", "date_from": "2025-04-01", "date_to": "2025-06-30"})
   → verified_total: "9805.00" INR, 8 transactions, possibly_missing_uncategorized_count: 0
4. get_sources({"transaction_ids": [...8 ids...]})
   → full provenance (file, page, raw text) for each
```

Final answer: `VERIFIED_WITH_CAVEATS`, ₹9,805.00, with a caveat explicitly noting the ledger's data
only starts 2025-05-14 — so the Apr 1–May 13 slice of the requested quarter can't be confirmed absent.
That caveat is the model's own observation from comparing step 1's requested range against step 2's
actual coverage; the verifier didn't have to force it, but rule 4 in the system prompt asks for
exactly this comparison before answering any period-scoped question.

### 4.4 A second real trace — where two things fire together

*"What's my biggest expense?"*:

```
1. search_transactions({"sort_by": "amount_desc", "limit": 1})
   → GRANDEUR JEWELLERS PVT, ₹80,000.00 — the single largest transaction
2. aggregate_spending({"group_by": "merchant"})
   → same merchant is also the highest merchant total (trivially, it's one transaction)
3. aggregate_spending({"group_by": "category"})
   → Shopping ₹28,097.24 is the highest CATEGORIZED total —
     but the ₹80,000 transaction itself has category=None, so it's invisible here
```

The final answer presents all three readings side by side (per the EC-22 fix) *and* explicitly flags
that the category-based reading excludes the ₹80,000 transaction because it's uncategorized (the
EC-26 completeness signal) — two separate fixes, both firing correctly in one real answer, neither
one hiding what the other found.

---

## 5. The trust boundary, restated plainly

The one sentence that explains most of the design choices above: **the LLM is only ever asked to
choose which deterministic function to call and to narrate the result — every number that reaches a
user was computed by plain Python, and every citation is checked against what the model actually
looked up this conversation before it's shown to anyone.** Everything else — the tiered extraction,
the economic-type ontology, the duplicate/anomaly flags, the verifier — exists to make that one
sentence actually true under adversarial and messy real-world input, not just in the happy path.
