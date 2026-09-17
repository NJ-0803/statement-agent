"""Optional: asks Groq's free API which spending category a merchant belongs to, for merchants nothing else
can place (file labels, your rules and memory, the built-in word lists).

Privacy: only cleaned merchant words are sent (categories.merchant_key — numbers, reference codes and
payment-rail codes already removed), never amounts, dates, account or card numbers. Each merchant is asked
about once; answers are remembered in merchant_knowledge. Answers must be valid JSON naming only merchants
that were asked about, with a short category; anything else is discarded. Rate limits, network errors and a
missing key never block an import — the merchant just stays uncategorized.

Enabled when GROQ_API_KEY is set (and STATEMENT_AGENT_GROQ isn't "off"). GROQ_MODEL picks the model; if it
has been retired, the first available Llama/Qwen/GPT-OSS chat model is used instead.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

API = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "llama-3.3-70b-versatile"
BATCH = 40
MAX_MERCHANTS_PER_IMPORT = 200
TIMEOUT = 20

_PROMPT = (
    "You categorize personal-finance transactions. For each merchant description, pick the single best "
    "spending category. Prefer one of these existing categories: {categories}. Only if none fits, give a new "
    "short category name (1-3 words, Title Case). If you can't tell, use \"Other\". "
    "Reply with JSON only: {{\"results\": [{{\"merchant\": <exactly as given>, \"category\": <name>}}]}}."
)


def groq_enabled() -> bool:
    return bool(os.environ.get("GROQ_API_KEY")) and os.environ.get("STATEMENT_AGENT_GROQ", "").lower() != "off"


def _request(path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{API}{path}", data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}", "Content-Type": "application/json",
                 "User-Agent": "statement-agent"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 - fixed https endpoint
        return json.loads(resp.read().decode())


def _fallback_model() -> str | None:
    try:
        ids = [m["id"] for m in _request("/models").get("data", []) if m.get("active", True)]
    except Exception:  # noqa: BLE001
        return None
    for prefix in ("llama-3", "openai/gpt-oss", "qwen", "llama"):
        for mid in sorted(ids, reverse=True):
            if mid.startswith(prefix) and "guard" not in mid and "whisper" not in mid:
                return mid
    return None


def _ask(model: str, merchants: list[str], categories: list[str]) -> dict:
    return _request("/chat/completions", {
        "model": model, "temperature": 0, "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _PROMPT.format(categories=", ".join(categories))},
            {"role": "user", "content": json.dumps({"merchants": merchants})},
        ],
    })


def _parse(payload: dict, asked: set[str]) -> dict[str, str]:
    content = payload["choices"][0]["message"]["content"]
    data = json.loads(content)
    out = {}
    for item in data.get("results", []) if isinstance(data, dict) else []:
        if not isinstance(item, dict):
            continue
        merchant, category = item.get("merchant"), item.get("category")
        if merchant not in asked or not isinstance(category, str):
            continue
        category = " ".join(category.split())
        if not category or len(category) > 40 or not re.fullmatch(r"[\w&' ,./-]+", category):
            continue
        out[merchant] = category
    return out


def suggest_categories(merchants: list[str], categories: list[str]) -> tuple[dict[str, tuple[str, str]], str | None]:
    """{merchant_key: (category, model)} for the merchants Groq could answer, plus a plain-language note when
    something stopped it (shown with the import, never raised)."""
    if not groq_enabled() or not merchants:
        return {}, None
    model = os.environ.get("GROQ_MODEL") or DEFAULT_MODEL
    answers: dict[str, tuple[str, str]] = {}
    todo = merchants[:MAX_MERCHANTS_PER_IMPORT]
    note = None if len(merchants) <= MAX_MERCHANTS_PER_IMPORT else (
        f"Groq was asked about the first {MAX_MERCHANTS_PER_IMPORT} new merchants only; the rest stay uncategorized for now.")
    for start in range(0, len(todo), BATCH):
        batch = todo[start:start + BATCH]
        try:
            try:
                payload = _ask(model, batch, categories)
            except urllib.error.HTTPError as e:
                if e.code in (400, 404) and "model" in e.read().decode(errors="replace").lower():
                    fallback = _fallback_model()
                    if not fallback:
                        raise
                    model = fallback
                    payload = _ask(model, batch, categories)
                else:
                    raise
            for merchant, category in _parse(payload, set(batch)).items():
                answers[merchant] = (category, f"groq:{model}")
        except urllib.error.HTTPError as e:
            reason = "its free-tier limit was reached" if e.code == 429 else f"it returned an error ({e.code})"
            return answers, f"Groq couldn't categorize some merchants because {reason}; they stay uncategorized for now."
        except (urllib.error.URLError, TimeoutError, OSError):
            return answers, "Groq couldn't be reached, so new merchants stay uncategorized for now."
        except (ValueError, KeyError, IndexError, TypeError):
            continue  # a malformed answer for one batch is discarded, not guessed at
    return answers, note
