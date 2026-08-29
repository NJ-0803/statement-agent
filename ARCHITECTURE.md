# ARCHITECTURE.md

A clean, non-chronological reference: what's built, why each piece is shaped the way it is, and
exactly what happens when a question is asked. `DECISIONS.md` is the session-by-session log of how
this came together (useful for "why did this change mid-build"); this document is the map of the
finished thing.

---

## 1. The whole system in one picture

```
                         ┌─────────────────────────┐
  dataset_public/  ───▶  │   INGESTION PIPELINE     │
  (PDF + CSV)            │   (ingest/pipeline.py)   │
                         └────────────┬─────────────┘
                                      │
                    ┌─────────────────┼─────────────────┐
                    ▼                 ▼                 ▼
              csv_parser.py    pdf_native.py      pdf_vision.py
              (CSV rows)      (pdfplumber,        (Claude vision,
                               anchor-based)        only for pages
                                    │                that fail a
                                    │                quality check)
                                    └────────┬────────┘
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

### 2.3 Extraction (`ingest/csv_parser.py`, `pdf_native.py`, `pdf_vision.py`, `quality.py`)

**What:** CSV rows are parsed via header-alias matching (`Txn Date` and `date` both map to the date
column) so differently-shaped sheets don't need separate code paths. PDF rows are reconstructed from
`pdfplumber` word *positions* (`(x0, top)` coordinates), not raw text-stream order — grouped by `top`
or 2pt tolerance, sorted by `x0` within a row, then classified by **anchors**: a date pattern at the
line start and an amount pattern at the line end. Anything matching neither anchor is metadata, never
a transaction. Pages where this yields nothing usable (empty text layer, or text with zero recognized
rows) get rasterized and sent to Claude's vision model as a fallback, tagged at lower confidence.

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

### 2.5 The ledger (`store.py`)

**What:** SQLite, two tables (`documents`, `transactions`), amounts stored as `TEXT` and parsed back
through `Decimal()`. `has_document(file_hash)` makes re-ingesting an unchanged folder a no-op.

**Why SQLite over a heavier option (DuckDB was specifically considered and rejected):** at
~90 transactions across 7 files, a columnar analytical engine buys nothing and costs a dependency;
`sqlite3` needs zero installation. The `Decimal`-safety property lives in how `store.py` reads/writes
values, not in which engine sits underneath — so this choice doesn't trade away any correctness.

### 2.6 The agent (`agent/tools.py`, `verifier.py`, `prompts.py`, `loop.py`)

Covered in detail in §3 below, since this is what you specifically asked about.

### 2.7 Two front ends, one core (`cli.py`, `web/app.py`)

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

## 3. The agent loop and tool calls — what actually happens when you ask a question

### 3.1 The ten tools

Everything the model can do is one of these. Nine return data; the tenth ends the turn.

| Tool | Purpose | Never does |
|---|---|---|
| `dataset_coverage` | Actual min/max date and currencies in the ledger | Guess at coverage the ledger doesn't have |
| `resolve_period` | Turns `"last_quarter"`, `"last_month"`, `"2025-Q2"` etc. into exact ISO start/end dates, correctly handling the year-boundary case | Let the model compute a date range by hand |
| `list_documents` | Every source file, its declared account/currency/period, and any ingest-time `warnings` (security flags, structural anomalies) | Require a bank name to appear as a merchant string to be findable |
| `search_transactions` | Raw filtered rows, with `sort_by`/`limit` for "the single largest transaction" | Compute any total |
| `aggregate_spending` | The only way to get a spend number — per-currency `verified_total`/`uncertain_total`, optional `group_by`, and `possibly_missing_uncategorized_count` when filtered by category | Blend currencies, or hide flagged/uncategorized transactions silently |
| `compare_periods` | Two `aggregate_spending` calls side by side | — |
| `find_disputable_transactions` | Every duplicate-flagged or anomaly-flagged row across the ledger | Declare anything fraud |
| `summarize_statement` | Full breakdown for one source file (by currency, by category, flagged count) | — |
| `get_sources` | Full provenance detail for a specific list of transaction IDs | — |
| `final_answer` | **Terminal.** The only way a turn ends. | Get shown to the user without passing the verifier first |

### 3.2 The loop, step by step (`agent/loop.py::run_agent`)

1. The system prompt (`agent/prompts.py`) + the 9 data tools + `final_answer` are sent to Claude along
   with the user's question.
2. Claude responds with one or more `tool_use` blocks. Each one is dispatched to the matching Python
   function in `agent/tools.py`, run against the in-memory ledger, and the result is serialized back
   as a `tool_result`. This repeats — Claude can call several tools across several turns before it's
   ready to answer (real traces run 1 to 5 tool calls deep).
3. Every tool call and its result is appended to a `trace: list[ToolCallRecord]` that persists for the
   *whole* conversation, not just the current turn.
4. When Claude calls `final_answer`, the proposed answer — `answer_text`, `proposed_status`,
   `verified_amounts`, `cited_transaction_ids`, `caveats` — is handed to `agent/verifier.py::verify()`,
   which runs **without calling the LLM at all**:
   - every `cited_transaction_ids` entry must be an ID that actually appeared in a tool result this
     conversation (never invented or half-remembered);
   - every `verified_amounts` entry must literally match (after `Decimal` normalization) a number that
     appeared somewhere in the trace — never something the model computed itself;
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

### 3.3 A real trace, annotated

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

### 3.4 A second real trace — where two things fire together

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

## 4. The trust boundary, restated plainly

The one sentence that explains most of the design choices above: **the LLM is only ever asked to
choose which deterministic function to call and to narrate the result — every number that reaches a
user was computed by plain Python, and every citation is checked against what the model actually
looked up this conversation before it's shown to anyone.** Everything else — the tiered extraction,
the economic-type ontology, the duplicate/anomaly flags, the verifier — exists to make that one
sentence actually true under adversarial and messy real-world input, not just in the happy path.
