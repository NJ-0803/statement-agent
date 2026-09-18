# Implementation status against the completion brief

Brief: *Statement Agent Completion Brief — Claude Code instructions and essential acceptance tests*,
reviewed at commit `ce2ba995`. This file is the checklist that brief's "definition of done" asks for.
Anything marked **not built** is not built; nothing here is marked done on the strength of a plan.

Test suite: 546 tests, offline, no API key needed (`python -m pytest`). Live checks that were **not** run
are listed at the bottom.

## Priorities

| Priority | Item | Status | Where |
| --- | --- | --- | --- |
| P0 | Bind each claim to its tool result: amount, currency, period, account, category, gross/net | **Built** | `agent/verifier.py`, `tests/test_verifier.py` (`TestClaimsAreBoundToTheirEvidence`), DECISIONS §42 |
| P0 | Render financial numbers from validated fields, including whole numbers | **Built** | prose money check in `verifier.py`; whole numbers with a currency now need backing |
| P0 | Reject mismatched or missing evidence | **Built** | same; a claim whose supporting number came back in another currency fails |
| P0 | Locale-aware amounts, full-cell validation, Decimal | **Built** | `normalize.py: normalize_amount / split_amount_digits`, `tests/test_normalize.py: TestLocaleAwareAmounts` |
| P0 | Ask when separators/dates/currencies are ambiguous | **Built** | checks `amount_format_assumed`, `date_order_assumed`, `currency_assumed`, `sign_convention_assumed` |
| P1 | Distinguish running balances from amounts | **Built for headed PDFs** (column reading, §38) and spreadsheets; a PDF with no header row still uses the date-first/amount-last rule | `ingest/pdf_columns.py` |
| P1 | Expose skipped rows/pages | **Built** | every row ends as transaction / ignored-with-reason / issue; "Rows I didn't use" in the review screen |
| P1 | Reconcile stated totals | **Built** | `statement_totals.py`, `resolve.reconcile_document` (§34) |
| P1 | Missing spreadsheet formula caches | **Built** | `sniff.find_uncached_formulas`, `formulas_without_results` check |
| P1 | Original-source viewer for cited pages/rows | **Not built** | citations carry file/page/row and the extracted text, but nothing renders the original page |
| P1 | Stable account identity | **Not built** | `account_name` per row only; no account record, no identity across files (`NOT_IMPLEMENTED.md` §H) |
| P1 | Transaction vs posted date | **Partly** | `value_date` is kept separate from `transaction_date`; there is no documented policy for which drives period totals |
| P1 | Pending-to-posted matching | **Not built** | — |
| P1 | Explicit cross-month refund treatment | **Built** | `net_spending` attributes a refund to its purchase's month and category, and lists what it did not subtract |
| P1 | Validate existing links and corrections | **Built** | `tests/test_linking.py`, `tests/test_corrections.py`, brief cases in `tests/test_brief_acceptance.py` |
| P1 | EMI and FX settlement links | **Not built** (deliberately: no tested policy) | — |
| P1 | Disclose and control external AI calls | **Built** | Groq is off unless `GROQ_API_KEY` is set; `/api/status` reports it; only cleaned merchant words are sent; answers marked "from Groq" |
| P1 | Minimize sensitive context and logs | **Partly** | card-number columns masked at intake; only merchant words leave the machine; no log redaction pass, and uploads/ledger are unencrypted |
| P1 | Bound parser size, time, memory, queue | **Partly** | 25 MB per file, 20 files per request, 200k rows, 200 PDF pages, zip-ratio limits, per-client rate limits; **no** per-file CPU/memory/time limit or isolated worker process |
| P1 | Safe exports, cancellation, deletion, backup/restore | **Built** | formula-safe CSV export, cancel/rollback, delete everything, `backup`/`restore`/`wipe` (§40) |
| P1 | Independent evaluation | **Built (harness), partly run** | `eval/question_bank.json`, `eval/run_red_team_bank.py --resume`, `eval/grade.py`; 74 of 95 answered, 21 blocked by API credit |
| P1 | Accessible onboarding; keyboard, screen reader, zoom, mobile | **Not verified** | built to the guidelines (18px base, 48px targets, labels, live regions, no dialogs); never tested with a screen reader or on a phone |
| P1 | Document supported formats/languages and uncertainty | **Built** | README "What it can't do (yet)", this file, `NOT_IMPLEMENTED.md` |
| P1 | Update stale README claims | **Built** | README test count, capabilities and limits updated |
| P2 | Authentication, per-owner authorization, private storage, durable jobs, quotas, monitoring, recovery tests | **Not built** | local mode only; no public hosting is authorized by the brief |

## Acceptance cases

All nine rows of the brief's table are tests in `tests/test_brief_acceptance.py` (plus the verifier and
locale cases in their own files). The four failures the brief reported were reproduced first:

| Case | Status |
| --- | --- |
| Wrong currency or scope | Reproduced, then fixed |
| Unsupported whole number | Reproduced, then fixed |
| European amount formatting | Reproduced, then fixed |
| Duplicate vs repeat purchase | **Reproduced a real totals bug**: two same-day INR 250 coffees read as 250. Same-statement duplicates now need a shared reference; separate lines are separate purchases, still flagged for a look |
| Payment and refund accounting | Passing |
| Missing or misread data | Passing (coverage gaps; balance breaks catch a misread digit) |
| Prompt injection and unsafe content | Passing (instructions are inert; exported formulas are prefixed; HTML is data, never markup) |
| Isolation and resource abuse | Local file-level isolation only; bounded errors covered. Hosted isolation is P2, not built |
| Crash retry and deletion | Passing (atomic commit, interrupted jobs failed on restart, rollback is final) |

## Release decisions still with the owner

- **Licence choice for this repository** — none is set.
- **PyMuPDF licensing**: PyMuPDF is AGPL-3.0 (or a commercial licence from Artifex). It is used for PDF
  page reading. Distributing this project publicly under a non-AGPL licence needs that resolved.
- **Dependency/licence inventory**: `requirements.txt` is the current list; a full licence inventory has
  not been compiled.
- **Provider data flow**: Anthropic (vision OCR for scanned pages, and the question-answering agent) and
  optionally Groq (merchant categorization). Their retention terms have not been confirmed in writing.
- **Deployment**: no public hosting is authorized, and none is set up.

## Not run

- The 21 evaluation questions blocked by API credit, and any live re-run of the other 74.
- Any live Anthropic vision-OCR run since the scanned-page path was last touched.
- Screen reader, keyboard-only, zoom and mobile checks.
- Any test against a real bank export: every fixture here is synthetic.
