"""Keep financial detail out of the logs.

On a laptop the logs are yours. On a host they are read by whoever runs the box, shipped to whatever
the platform collects, and kept long after the data itself is gone. Two things leak there by default:

  * the request line, which carries query strings — `?q=swiggy` is a merchant someone searched for
  * anything a traceback or a warning happens to quote, including amounts and account numbers

So the query string is dropped, long digit runs are masked, and anything that looks like an amount is
replaced. This runs as a logging filter, which means it applies to werkzeug's access log and to the
app's own logger without either of them having to remember.

It is a safety net, not a licence to log sensitive things deliberately.
"""

from __future__ import annotations

import logging
import re

# 6+ digits in a row: account numbers, card numbers, long reference numbers. Shorter runs are dates,
# row counts and amounts under a lakh, which are handled separately or not worth mangling.
_LONG_DIGITS = re.compile(r"\b\d[\d\s-]{4,}\d\b")
# a currency symbol or code followed by a number, in either order
_MONEY = re.compile(
    r"(?:(?:₹|\$|€|£|¥)\s?\d[\d,._]*(?:\.\d+)?)|(?:\b(?:INR|USD|EUR|GBP|JPY|AUD|CAD|SGD|AED)\s?\d[\d,._]*\b)",
    re.IGNORECASE,
)
# "GET /api/transactions?q=swiggy HTTP/1.1" -> the part after ? is the visitor's own words
_QUERY = re.compile(r"(\s|\")(/[^\s\"?]*)\?[^\s\"]*")


def redact(text: str) -> str:
    """Mask the things in `text` that should not survive in a log line."""
    if not text:
        return text
    text = _QUERY.sub(r"\1\2?<hidden>", text)
    text = _MONEY.sub("<amount>", text)
    return _LONG_DIGITS.sub("<digits>", text)


class RedactingFilter(logging.Filter):
    """Rewrites a record's message in place. Returns True always: this hides detail, never lines."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging's own name
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: redact(v) if isinstance(v, str) else v for k, v in record.args.items()}
                else:
                    record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        except Exception:  # noqa: BLE001 - a logging filter must never be the thing that breaks a request
            return True
        return True


def install(*logger_names: str) -> None:
    """Attach the filter to the loggers that carry request and application detail."""
    names = logger_names or ("werkzeug", "statement_agent", "gunicorn.access", "gunicorn.error")
    redacting = RedactingFilter()
    for name in names:
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactingFilter) for f in logger.filters):
            logger.addFilter(redacting)
