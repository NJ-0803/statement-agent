# Implementation status against the completion brief

Brief: *Statement Agent Completion Brief — Claude Code instructions and essential acceptance tests*,
reviewed at commit `ce2ba995`. This file is the checklist that brief's "definition of done" asks for.
Anything marked **not built** is not built; nothing here is marked done on the strength of a plan.

Test suite: 592 tests, offline, no API key needed (`python -m pytest`). Live checks that were **not** run
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
| P1 | Original-source viewer for cited pages/rows | **Out of scope** — owner's decision, 18 Sep 2026 | citations still carry file, page, row and the extracted text; nothing renders the original page |
| P1 | Stable account identity | **Out of scope** — owner's decision, 18 Sep 2026 | `account_name` per row is kept; no account record or cross-file identity |
| P1 | Transaction vs posted date | **Partly** | `value_date` is kept separate from `transaction_date`; there is no documented policy for which drives period totals |
| P1 | Pending-to-posted matching | **Out of scope** — owner's decision, 18 Sep 2026 | — |
| P1 | Explicit cross-month refund treatment | **Built** | `net_spending` attributes a refund to its purchase's month and category, and lists what it did not subtract |
| P1 | Validate existing links and corrections | **Built** | `tests/test_linking.py`, `tests/test_corrections.py`, brief cases in `tests/test_brief_acceptance.py` |
| P1 | EMI and FX settlement links | **Out of scope** — owner's decision, 18 Sep 2026 | — |
| P1 | Disclose and control external AI calls | **Built** | Groq is off unless `GROQ_API_KEY` is set; `/api/status` reports it; only cleaned merchant words are sent; answers marked "from Groq" |
| P1 | Minimize sensitive context and logs | **Partly** | card-number columns masked at intake; only merchant words leave the machine; query strings, amounts and long digit runs are redacted from logs off localhost (`web/redact.py`). Uploads and the ledger are still unencrypted at rest |
| P1 | Bound parser size, time, memory, queue | **Partly; the rest is out of scope** — owner's decision, 18 Sep 2026 | 25 MB per file, 20 files per request, 200k rows, 200 PDF pages, zip-ratio limits, per-client rate limits. Per-file CPU/memory/time limits and isolated worker processes are not being built |
| P1 | Safe exports, cancellation, deletion, backup/restore | **Built** | formula-safe CSV export, cancel/rollback, delete everything, `backup`/`restore`/`wipe` (§40) |
| P1 | Independent evaluation | **Harness built; run stays partial by the owner's decision** | `eval/question_bank.json`, `eval/run_red_team_bank.py --resume`, `eval/grade.py`; 74 of 95 answered, and the 21 blocked by API credit will not be run |
| P1 | Accessible onboarding; keyboard, screen reader, zoom, mobile | **Keyboard, zoom and contrast checked and fixed; screen reader and a real phone still not tested** | keyboard/reflow/contrast pass of 18 Sep 2026 below; `tests/test_accessibility.py` |
| P1 | Document supported formats/languages and uncertainty | **Built** | README "What it can't do (yet)", this file, `NOT_IMPLEMENTED.md` |
| P1 | Update stale README claims | **Built** | README test count, capabilities and limits updated |
| P2 | Authentication, per-owner authorization, private storage, durable jobs, quotas, monitoring, recovery tests | **Partly built** — the owner reversed the 18 Sep "not planned" decision to host a public demo | Authentication, a deny-by-default route gate, an isolated demo mode, log redaction and a shared daily API allowance are built (`web/auth.py`, `web/demo.py`, `web/redact.py`, `DEPLOY.md`). Per-owner authorization, encryption at rest, durable jobs and monitoring are not |

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

## Accessibility pass, 18 Sep 2026

Run in Chrome against the real UI with 897 transactions loaded. Each failure was reproduced in the
browser before it was fixed, and each fix has a test in `tests/test_accessibility.py` that fails
without it.

Fixed:

| What was wrong | WCAG 2.2 AA |
| --- | --- |
| The first tab stop on the page — "Skip to content" — stayed clipped to 1×1px when focused, so the first Tab press moved focus somewhere invisible | 2.4.7 Focus Visible |
| The two file inputs ("Choose statement files", "Take a photo") are visually hidden but still tab stops: focus landed on the main action of the app with nothing on screen | 2.4.7 Focus Visible |
| Input, select, fieldset, quiet-button and drop-zone outlines sat at 1.61:1 against their background in both themes | 1.4.11 Non-text Contrast |
| Opening a transaction's editor deleted the button holding focus, dropping focus to `<body>`; closing or saving it did the same | 2.4.3 Focus Order |
| Finishing a batch of files called `.focus()` on the drop zone, which is a `<label>` and cannot take focus, so the call did nothing | 2.4.3 Focus Order |

Checked and already passing: text contrast on every colour pair in both themes (lowest 5.72:1 against
a 4.5:1 requirement); reflow at a simulated 320px with no horizontal scrolling; the 1.4.12 text-spacing
overrides with no clipping; every control has an accessible name; heading order; no duplicate ids; no
positive `tabindex`; scrollable tables reachable by keyboard (`role="region"` with `tabindex="0"`).

Still not checked: a screen reader (VoiceOver/NVDA) and a real phone. Both need the owner.

## Explicitly out of scope (owner's decision, 18 Sep 2026)

Asked for by the brief, declined by the owner — not deferred, not forgotten:

- Stable account identity across files.
- Pending-to-posted matching.
- EMI and FX settlement links.
- Per-file CPU / memory / time limits and isolated parser worker processes.
- An original-source viewer for cited pages and rows.
- Re-running the 21 evaluation questions that API credit blocked: the run stays at 74 of 95, and
  `eval/report.md` reports those 21 as infrastructure errors, never as passes or failures.
- ~~All P2 hosting work.~~ **Reversed on 18 Sep 2026**: the owner decided to host a public demo, so
  authentication, demo isolation, log redaction and an API spend cap were built. What is still not
  built is per-owner authorization, encryption at rest, durable jobs and monitoring — see `DEPLOY.md`.

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

- The 21 evaluation questions blocked by API credit, and any live re-run of the other 74 — out of scope
  by the owner's decision, so the recorded result stands at 74 answered of 95.
- Any live Anthropic vision-OCR run since the scanned-page path was last touched.
- Screen reader (VoiceOver/NVDA) and a check on a real phone. The keyboard, zoom/reflow and contrast
  checks were run on 18 Sep 2026 — see the accessibility pass above.
- Any test against a real bank export: every fixture here is synthetic.
