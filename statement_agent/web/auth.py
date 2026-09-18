"""Who is allowed in, and how the app behaves depending on where it is running.

Three modes, chosen with STATEMENT_AGENT_MODE:

  local (default)  What this has always been: your own machine, your own ledger, no login. Nothing
                   here changes that — the gate below lets every request through.
  owner            Your ledger, reachable over a network. One passphrase, and every route is denied
                   until you have entered it.
  demo             A public instance for people to try. It refuses to open a real ledger at all
                   (see demo.py) and each visitor gets their own throwaway copy of the sample data.

The passphrase is stored only as an scrypt hash, in a file outside the repo or in an environment
variable. scrypt is in the standard library, so this adds no dependency and no build step.

The gate is deny-by-default on purpose: a route added later is protected without anyone remembering
to protect it. Only the paths in PUBLIC_PATHS are open, and that list is short enough to read.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

# scrypt, at the cost the standard library documents for interactive logins. n is the work factor;
# raising it makes both a login and an offline guessing attack proportionally slower.
_N, _R, _P = 2 ** 15, 8, 1
_SALT_BYTES, _KEY_BYTES = 16, 32
# scrypt needs roughly 128 * n * r bytes; OpenSSL refuses anything over 32 MB unless told otherwise,
# and these parameters need exactly that, so the ceiling is raised with a little headroom.
_MAXMEM = 128 * _N * _R * 2

MODES = ("local", "owner", "demo")

# Paths reachable without a session. Everything else needs one.
PUBLIC_PATHS = frozenset({"/login", "/logout", "/healthz"})
PUBLIC_PREFIXES = ("/static/",)


def mode() -> str:
    """Which mode this process is running in. Anything unrecognised falls back to the safest one."""
    value = (os.environ.get("STATEMENT_AGENT_MODE") or "local").strip().lower()
    return value if value in MODES else "owner"


def needs_login() -> bool:
    return mode() == "owner"


def hash_passphrase(passphrase: str) -> str:
    """A passphrase as it is stored: scrypt, with its parameters and salt written alongside, so the
    cost can be raised later without stranding existing stored values."""
    if not passphrase:
        raise ValueError("The passphrase cannot be empty.")
    salt = secrets.token_bytes(_SALT_BYTES)
    key = hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P,
                         dklen=_KEY_BYTES, maxmem=_MAXMEM)
    return "$".join(["scrypt", str(_N), str(_R), str(_P),
                     base64.b64encode(salt).decode(), base64.b64encode(key).decode()])


def verify_passphrase(passphrase: str, stored: str) -> bool:
    """True when the passphrase matches. Never raises on a malformed stored value: a corrupted or
    truncated credentials file must read as "wrong passphrase", not as a crash or a way in."""
    try:
        scheme, n, r, p, salt_b64, key_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(key_b64)
        candidate = hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=int(n), r=int(r),
                                   p=int(p), dklen=len(expected), maxmem=128 * int(n) * int(r) * 2)
    except (ValueError, TypeError, KeyError):
        return False
    return hmac.compare_digest(candidate, expected)


def credentials_path() -> str:
    """Where the passphrase hash lives. Deliberately outside the repo by default, so it cannot be
    committed by accident."""
    override = os.environ.get("STATEMENT_AGENT_CREDENTIALS")
    if override:
        return os.path.abspath(override)
    return os.path.join(os.path.expanduser("~"), ".statement-agent", "credentials.json")


def stored_hash() -> str | None:
    """The stored passphrase hash, from the environment first (how a host supplies it) and then the
    credentials file (how a laptop does). None when no passphrase has been set."""
    from_env = os.environ.get("STATEMENT_AGENT_PASSPHRASE_HASH")
    if from_env:
        return from_env.strip()
    path = credentials_path()
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f).get("passphrase_hash")
    except (OSError, json.JSONDecodeError):
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def save_passphrase(passphrase: str) -> str:
    """Write a new passphrase hash, readable only by this user. Returns the file it was written to."""
    path = credentials_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    payload = {"passphrase_hash": hash_passphrase(passphrase), "set_at": int(time.time())}
    # write private from the start, rather than creating it readable and narrowing afterwards
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return path


def secret_key() -> tuple[bytes, bool]:
    """The key that signs session cookies, and whether it is a durable one.

    Supplied by the host in owner or demo mode. Without it sessions cannot outlive a restart, which
    is fine on a laptop and not fine on a server — the caller warns rather than failing silently.
    """
    provided = os.environ.get("STATEMENT_AGENT_SECRET_KEY")
    if provided and len(provided) >= 32:
        return provided.encode("utf-8"), True
    return secrets.token_bytes(32), False


def is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


class LoginThrottle:
    """Slows down passphrase guessing. Counted per client address, in memory.

    Not a defence against a distributed attacker — scrypt is what makes guessing expensive. This
    stops a single host from trying thousands of passphrases a minute.
    """

    def __init__(self, limit: int = 8, window_seconds: int = 300) -> None:
        self.limit, self.window = limit, window_seconds
        self._hits: dict[str, list[float]] = {}

    def _recent(self, client: str, now: float) -> list[float]:
        return [t for t in self._hits.get(client, []) if now - t < self.window]

    def blocked(self, client: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        recent = self._recent(client, now)
        self._hits[client] = recent
        return len(recent) >= self.limit

    def record_failure(self, client: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._hits[client] = self._recent(client, now) + [now]

    def clear(self, client: str) -> None:
        self._hits.pop(client, None)

    def seconds_until_retry(self, client: str, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        recent = self._recent(client, now)
        if len(recent) < self.limit:
            return 0
        return max(1, int(self.window - (now - min(recent))))
