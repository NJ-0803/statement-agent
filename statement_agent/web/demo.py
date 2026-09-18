"""Demo mode: a public instance people can try without seeing anyone's real statements.

Two separate problems, and this module exists because getting either wrong is expensive:

1. The owner's ledger must not be reachable. Not "not linked to" — not reachable. In demo mode the
   database path is never taken from configuration; it is always built from the visitor's session id
   underneath the demo root, and `guard_path` refuses anything that resolves outside it. Pointing a
   demo instance at a real ledger raises on the way in rather than serving it.

2. Visitors will upload their own real bank statements. That is what a demo of this invites, and it
   makes whoever runs it the custodian of other people's financial data. So each visitor gets a
   private sandbox that is deleted on a timer, and the page says so before they choose a file.

Every sandbox starts as a copy of a seed ledger built from dataset_public/, which is synthetic.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time

SESSION_KEY = "demo_sandbox"
# long enough to read a statement and ask a few questions, short enough that an uploaded file is not
# sitting on a public host hours later
SANDBOX_TTL_SECONDS = int(os.environ.get("STATEMENT_AGENT_DEMO_TTL", "3600"))
_SAFE_ID = re.compile(r"\A[0-9a-f]{32}\Z")


def demo_root() -> str:
    """Where visitor sandboxes live. Under the system temp dir by default, so a restart of the host
    cannot leave them lying around indefinitely."""
    override = os.environ.get("STATEMENT_AGENT_DEMO_ROOT")
    root = os.path.abspath(override) if override else os.path.join(tempfile.gettempdir(), "statement-agent-demo")
    os.makedirs(root, mode=0o700, exist_ok=True)
    return root


def seed_path() -> str:
    """The prebuilt synthetic ledger every sandbox is copied from (`cli build-demo-seed` makes it)."""
    override = os.environ.get("STATEMENT_AGENT_DEMO_SEED")
    if override:
        return os.path.abspath(override)
    return os.path.join(demo_root(), "seed.db")


def guard_path(path: str) -> str:
    """Return `path` if it is inside the demo root, otherwise refuse.

    This is the check that makes the separation structural. It runs on every database path a demo
    instance opens, so a misconfiguration surfaces as a loud error instead of a public ledger.
    """
    resolved = os.path.realpath(path)
    root = os.path.realpath(demo_root())
    if resolved != root and not resolved.startswith(root + os.sep):
        raise RuntimeError(
            f"Demo mode tried to open a ledger outside the demo area ({resolved}). Refusing: a demo "
            "instance must never be able to read a real ledger."
        )
    return resolved


def sandbox_dir(sandbox_id: str) -> str:
    if not _SAFE_ID.match(sandbox_id or ""):
        raise ValueError("Not a sandbox id.")  # never let a session value become a path segment
    return os.path.join(demo_root(), sandbox_id)


def create_sandbox(sandbox_id: str) -> str:
    """Give this visitor their own copy of the sample ledger. Returns the sandbox directory."""
    directory = sandbox_dir(sandbox_id)
    os.makedirs(os.path.join(directory, "uploads"), mode=0o700, exist_ok=True)
    ledger = os.path.join(directory, "ledger.db")
    if not os.path.exists(ledger):
        seed = seed_path()
        if os.path.exists(seed):
            shutil.copyfile(seed, ledger)
        # no seed yet: the visitor simply starts with an empty ledger and can add their own file
    return directory


def sandbox_paths(sandbox_id: str) -> tuple[str, str]:
    """(ledger path, upload directory) for a visitor, both guaranteed inside the demo root."""
    directory = create_sandbox(sandbox_id)
    return guard_path(os.path.join(directory, "ledger.db")), guard_path(os.path.join(directory, "uploads"))


def touch(sandbox_id: str) -> None:
    """Mark a sandbox as in use, so it is not swept while someone is still reading it."""
    try:
        os.utime(sandbox_dir(sandbox_id), None)
    except (OSError, ValueError):
        pass


def sweep(ttl_seconds: int = SANDBOX_TTL_SECONDS, now: float | None = None) -> int:
    """Delete sandboxes nobody has touched for `ttl_seconds`. Returns how many went.

    This is what limits how long a stranger's uploaded statement exists on the host.
    """
    now = time.time() if now is None else now
    root = demo_root()
    removed = 0
    for name in os.listdir(root):
        if not _SAFE_ID.match(name):
            continue
        path = os.path.join(root, name)
        try:
            if now - os.path.getmtime(path) < ttl_seconds:
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        except OSError:
            continue
    return removed
