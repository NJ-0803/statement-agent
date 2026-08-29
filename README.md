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

## Running the tests

```bash
python -m pytest tests/ -v
```

152 tests, all runnable offline with no API key — they cover normalization (currency/date parsing),
PDF/CSV extraction (including the injection-defense and duplicate-detection tests described below),
resolution (categorization, duplicates, reconciliation, anomaly detection), the query/aggregation
tools, and the answer verifier's grounding/provenance checks. See `DECISIONS.md` §11 for the three real
bugs found during development (two caught by this suite, one only found via live testing).

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

- Parse PDF bank/credit-card statements with varied layouts and CSV expense/reimbursement sheets.
- Normalize dates across ISO, `DD/MM/YYYY`, `MM-DD-YYYY`, and textual formats, resolving ambiguous
  numeric dates using other dates in the same document as context.
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
- Resist prompt injection embedded inside statement text — structurally (injected text can't become a
  transaction row in the first place; see `DECISIONS.md` §6), not just via a system-prompt request.
- Say "I don't have enough information" when the ledger doesn't cover the question (e.g. a date range
  outside every statement's period) rather than estimating.

## What it can't do (yet)

- No LLM-assisted fallback categorization — an unrecognized merchant gets `category: None` rather than
  a guessed category, which is correct-but-conservative (see `DECISIONS.md` §13 for what a soft-
  confidence version would add).
- No linking between related transactions (e.g. a refund isn't matched back to its original purchase)
  — none of the current sample data has real transfer/refund pairs to build and test this against.
- No web UI — CLI only.

The vision-OCR path and the live multi-turn agent loop have now been tested end-to-end against the
real dataset with live API credits — see `DECISIONS.md` §12 for the full set of live results, all
matching the independently hand-computed gold numbers in `eval/gold_qa.py`. One real gap was found this
way (a bank/statement referenced by name, like "the Cobalt statement," had no discovery path since a
bank name is a filename property, not a merchant string) and fixed with a new `list_documents` tool —
see `DECISIONS.md` §11 for the writeup, since it's a good example of the kind of bug only live testing
can find.
