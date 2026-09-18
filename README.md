# Statement Intelligence Agent

An agent that reads a folder of messy bank/credit-card statements (PDF) and expense sheets (CSV),
normalizes them into one ledger, and answers natural-language money questions — every number
traceable to a source, every uncertainty stated rather than hidden.

See `DECISIONS.md` for the full architecture writeup, rationale for every major choice, and an honest
list of what's unfinished. See `EDGE_CASES.md` for a line-by-line audit against a 49-item external
edge-case catalogue — what passes, what's partial, what's an acknowledged gap, and three real bugs it
found and fixed during the audit itself (including a genuine duplicate transaction hiding in
`dataset_public/` that no prior check could see).

## Setup

```bash
cd statement-agent
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and set ANTHROPIC_API_KEY=sk-ant-...
```

Requires Python 3.11+ (developed and tested on 3.12, arm64). Note: if you're on Apple Silicon and see
an `ImportError` about `cryptography`/OpenSSL symbols when creating the venv, it means a non-native
(Rosetta/x86_64) Python interpreter got picked up — recreate the venv with a native arm64 interpreter
(e.g. `/usr/local/bin/python3.12` or your Homebrew `python3`, whichever `file $(which python3)` shows
as `arm64`).

## Running

**1. Ingest the dataset into a local ledger** (no API key needed for this step):

```bash
python -m statement_agent.cli ingest
```

Runs against `dataset_public/` by default and writes `ledger.db`. Prints what was ingested, what was
skipped, and any warnings (extraction quality issues, security flags, flagged transactions). Add
`--no-vision` to skip the vision-OCR fallback entirely (useful if you don't have API credits yet, or
want a fast deterministic-only run) — the scanned statement will just contribute 0 transactions with
a clear warning rather than silently guessing at its contents.

```bash
python -m statement_agent.cli ingest --folder /path/to/other/dataset --db other_ledger.db --fresh
```

**2. Ask questions** (requires `ANTHROPIC_API_KEY`):

```bash
python -m statement_agent.cli ask "What did I spend on dining last quarter?"
python -m statement_agent.cli ask "Compare my grocery spending across months." --trace
python -m statement_agent.cli ask --interactive
```

`--trace` prints every tool call the agent made this turn, for observability. Every answer prints its
verification status (`VERIFIED` / `VERIFIED_WITH_CAVEATS` / `INSUFFICIENT_INFORMATION`), the amounts
it's standing behind, any caveats, and how many transactions it cited as sources.

**3. Or use the web UI instead of the terminal** (same underlying agent, requires `ANTHROPIC_API_KEY`):

```bash
python -m statement_agent.cli serve
```

Opens on `http://127.0.0.1:5050` with three areas:

- **Add statements** — a four-step guided flow: *Add files → Check what I found → Fix highlighted items →
  Done*. Files are read into a staged import that is **not** in your ledger yet. You see what was found
  (period, money in/out, how each column was read, rows that weren't used and why), can correct the column
  choices, and deal with anything uncertain one item at a time: confirm a currency, confirm a date format,
  leave a row out, or say whether a row is money in or out. Only then is the import added, in one atomic
  step.
- **Your statements** — every import, newest first. Any added import can be undone without touching the
  others.
- **Ask a question** — the same verified agent as the CLI. "Why?" under each answer lists the exact source
  rows it used.

`--port` to use a different port. The server is a thin wrapper over the same `Store`, import pipeline and
`run_agent()` the CLI uses. See `DECISIONS.md` §33 for the import design and `NOT_IMPLEMENTED.md` §I for
what it deliberately doesn't do yet (no accounts or authentication — it's a local, single-user tool).

**Undo an import from the terminal:**

```bash
python -m statement_agent.cli imports            # list import jobs and their ids
python -m statement_agent.cli rollback JOB_ID    # remove exactly that import's transactions
```

`cli ingest` uses the same staged pipeline non-interactively. It adds a file only when its columns are read
with certainty and it produced at least one transaction. Anything else is listed as *not added*, and
nothing is written for it.

## Running the tests

```bash
python -m pytest tests/ -v
```

553 tests, all runnable offline with no API key (they run on 4 processes by default; add `-n 0` for one)
— they cover normalization (currency/date parsing, including European 1.234,50 formats),
structure detection and column-role inference for unfamiliar bank exports, staged import commit/rollback,
PDF/CSV extraction (including the injection-defense and duplicate-detection tests described below),
resolution (categorization, duplicates, reconciliation, anomaly detection), the query/aggregation
tools, the answer verifier's evidence binding (currency, period, category and gross/net), links between
related transactions, categorization that learns from your files, and the completion brief's acceptance
cases in `tests/test_brief_acceptance.py`. See `DECISIONS.md` §11 for the three real bugs found during
development, and §34–§42 for what each later batch changed and why.

**Gold-answer eval harness** (`eval/gold_qa.py`) — the "don't trust your agent, verify it" artifact
specifically:

```bash
python eval/gold_qa.py
```

7 gold questions (matching the brief's own examples: dining spend, grocery comparison, disputable
charges, statement summary, out-of-range period) with expected numbers computed by hand independently
of the aggregation code, checked against what `dataset_public/` actually produces. Prints a pass/fail
report with the exact expected-vs-actual for each. Scope note: this validates the deterministic
computation layer only — it does not yet test whether the LLM correctly routes a natural-language
question to the right tool calls, since that requires a live API call (see `DECISIONS.md` §10). Also
runs as part of `pytest tests/` via `tests/test_gold_eval.py`, one test per case.

## What it can do

- Parse PDF bank/credit-card statements with varied layouts, CSV and XLSX expense/reimbursement
  sheets, and standalone statement images (a photographed or screenshotted page) via the same
  vision-OCR path used as a PDF fallback.
- Normalize dates across ISO, `DD/MM/YYYY`, `MM-DD-YYYY`, and textual formats, resolving ambiguous
  numeric dates using other dates in the same document as context — and the same disambiguation for a
  date typed directly in a question (e.g. "05/07/2026"), via a dedicated `resolve_date` tool, always
  disclosing when a locale-default guess was needed rather than silently picking one.
- Normalize merchant names for grouping and duplicate detection (stripping noise like a trailing "PVT
  LTD"), while always citing the original, unmodified merchant text from the source document.
- Track each transaction's real position in its source document's own row order, separate from its date —
  so a question about the document's own ordering ("is this statement sorted by date?") can be answered
  from the document's actual row sequence, not by re-sorting the results and checking the sort (which
  would trivially always look sorted regardless of the truth).
- Normalize currency across `₹`, `Rs.`, `INR`, `USD`, bare numbers, `CR`/`DR` suffixes, and
  parenthesized negatives — using exact `Decimal` arithmetic throughout, never floats.
- Fall back to vision-model OCR for pages with no extractable text (e.g. scanned statements), while
  keeping every vision-extracted value at lower confidence than natively-extracted values and running
  it through the same validation as everything else.
- Classify every transaction's economic type (purchase, transfer, card payment, refund, cash
  withdrawal, ...) before assigning a spend category, so a credit-card bill payment is never
  double-counted as both a bank debit and the card's own purchases.
- Flag probable duplicate charges and statistical outliers — flags them for review, never deletes or
  auto-resolves them.
- Detect ATM withdrawals, bank transfers (NEFT/IMPS/RTGS), fees, interest, and reversals from
  transaction text and exclude them from spend totals — not just purchases vs. generic credits.
- Resolve relative time periods ("last quarter", "last month") deterministically, including the
  year-boundary case (asking for "last quarter" in Q1 correctly resolves to Q4 of the previous year)
  — the agent never computes date arithmetic itself, same principle as never doing money arithmetic
  itself.
- Answer spend-by-category, month-over-month comparison, statement-summary, and "anything I should
  review" questions, with every number backed by a deterministic tool call (never LLM mental math)
  and every citation checked against what the agent actually looked up this conversation.
- Resolve a question naming a specific bank/card/statement (e.g. "the Cobalt statement") to the right
  source document even when that name never appears as a merchant string inside any transaction, and
  surface that document's ingest-time security/data-quality flags directly rather than making the
  agent re-derive them from raw data each time.
- Convert and combine multi-currency spend into one total on request (e.g. "total spend in July, in
  INR, including the USD charges") — every transaction converts using the real historical exchange
  rate quoted for its OWN date, from a bundled open-data rate file (ECB reference rates, not a live
  API call — see `DECISIONS.md` §17), with the converted figure always shown alongside, never instead
  of, the honest per-currency breakdown. Relevant for a card issuer's customers specifically: foreign-
  currency transactions happen even without leaving India.
- Resist prompt injection embedded inside statement text — structurally (injected text can't become a
  transaction row in the first place; see `DECISIONS.md` §6), not just via a system-prompt request.
- Say "I don't have enough information" when the ledger doesn't cover the question (e.g. a date range
  outside every statement's period) rather than estimating.
- Detect internal coverage gaps, not just outer date bounds — a quarter of statements never uploaded
  would otherwise look identical to full coverage; a question overlapping a detected gap gets an explicit
  caveat instead of a total that quietly looks complete.
- Process a multi-page scanned document's vision-OCR fallback concurrently (bounded, with retry/backoff
  on transient failures) rather than one page at a time with no retry.
- Log its own reasoning, not just tool names and inputs — every tool call carries a one-sentence "why"
  from the model alongside it, and the final answer carries a separate `final_reasoning`, both available
  via `--trace`, but never fed into the verifier's grounding checks (which only ever inspect actual tool
  results, so "confident reasoning" toward a fabricated number still fails verification).
- Check specific decimal figures stated in the answer's own prose (a statistical threshold, a percentage),
  not just the structured "verified" numbers — found live that a plausible-sounding number in free text
  passed verification purely by coincidence, since nothing had ever checked it; see `DECISIONS.md` §22.
- Answer questions needing a simple derived value (e.g. "the transaction closest to the average of my
  highest and lowest") deterministically — a `compute` tool for arithmetic over numbers already retrieved
  this turn, paired with `search_transactions(sort_by="closest_to_amount")`, rather than either doing the
  math itself or refusing a legitimately answerable question; see `DECISIONS.md` §27.
- Render bar/line/pie charts on request ("show me a chart of...", "visualize...") — from the exact same
  `aggregate_spending` grouped totals every text answer already uses, never a second computation path,
  and never blending currencies into one chart. Shown inline in the web UI and saved as a PNG file from
  the CLI; see `DECISIONS.md` §28.
- Answer "top N transactions in every category/merchant/month" in one deterministic call
  (`top_n_per_group`), rather than one `search_transactions` call per group — and, only when explicitly
  asked for a "dashboard," combine that with a chart in one inline view in the web UI (never a separate
  page); see `DECISIONS.md` §29.
- Serve more than one person's finances without ever blending them — `--client NAME` (resolved via a
  local, gitignored `clients.json`; see `clients.json.example`) points `ingest`/`ask`/`serve` at that
  client's own separate ledger `.db` file, so two people's transactions are never in the same in-memory
  ledger. Useful for a family tracking several members separately, or a CA with multiple clients; CLI/config
  only for now, no web UI switcher yet — see `DECISIONS.md` §31.
- Capture and use a source file's own `Account Name`/`Category` columns when it declares them (e.g. one
  person's own multiple cards, or a merchant taxonomy our keyword list doesn't recognize) — filterable and
  groupable (`account`/`category` on every aggregate tool), and falls back to the file's own category label
  only when our keyword matcher finds nothing, never overriding a confident match; see `DECISIONS.md` §32.
- Respect an explicit debit/credit column when a source declares one, instead of only inferring direction
  from the amount string's own sign — catches income rows (a paycheck, a refund) that would otherwise be
  silently counted as spend just because the amount had no minus sign or CR/DR suffix; see `DECISIONS.md` §32.

## What it can't do (yet)

- **No accounts or logins.** One ledger file is one person's finances; people are kept apart by file
  (`--client`), not by authentication. Nothing here is ready for shared hosting — see
  `IMPLEMENTATION_STATUS.md` and `NOT_IMPLEMENTED.md` §I.
- **No encryption at rest** and no retention policy: uploads and `ledger.db` sit unencrypted on this
  machine. `backup`, `restore` and `wipe` exist; a passphrase lock does not.
- **Some links aren't built:** EMI instalments to their original purchase, an FX markup fee to its
  foreign charge, and pending-to-posted matching (refunds, transfers, card bills and recurring payments
  are — `DECISIONS.md` §36).
- **PDFs without a table header** still use the date-first/amount-last row rule, so a balance column can
  be mistaken for the amount there (headed PDFs are read by column position, §38).
- **Category suggestions from Groq are optional and off by default**, and they are a model's guesses:
  they are applied, marked "from Groq", and correctable. Without a key, an unrecognised merchant simply
  has no category.
- **The 95-question evaluation bank has not been fully run:** 74 answered, 21 still blocked by API
  credit at the time of the saved run. `eval/grade.py` grades what exists and reports the blocked cases
  separately rather than as failures.

The vision-OCR path and the live multi-turn agent loop have now been tested end-to-end against the
real dataset with live API credits — see `DECISIONS.md` §12 for the full set of live results, all
matching the independently hand-computed gold numbers in `eval/gold_qa.py`. One real gap was found this
way (a bank/statement referenced by name, like "the Cobalt statement," had no discovery path since a
bank name is a filename property, not a merchant string) and fixed with a new `list_documents` tool —
see `DECISIONS.md` §11 for the writeup, since it's a good example of the kind of bug only live testing
can find.
