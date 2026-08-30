"""Multi-client ledger selection — future-scope plumbing for a CA-type user managing
several people's finances, not a single shared ledger with an account_id field.

Each client gets a completely separate ledger .db file; there is no code path that
ever reads two clients' transactions into the same in-memory ledger, so the
account-blending gap recorded in NOT_IMPLEMENTED.md cannot recur through this
mechanism. `--client NAME` is purely a convenience over passing `--db path.db`
directly — omitting it preserves today's single-ledger behavior exactly.
"""

from __future__ import annotations

import json
import os

DEFAULT_CLIENTS_CONFIG = "clients.json"
DEFAULT_DB_PATH = "ledger.db"


def load_clients(config_path: str = DEFAULT_CLIENTS_CONFIG) -> dict[str, str]:
    """Returns {} if no config file exists yet — having zero clients configured
    is a normal, valid state, not an error."""
    if not os.path.exists(config_path):
        return {}
    with open(config_path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
        raise ValueError(f"{config_path} must be a JSON object of {{client_name: db_path}} string pairs")
    return data


def resolve_db_path(
    *, client: str | None, db: str | None, config_path: str = DEFAULT_CLIENTS_CONFIG
) -> str:
    """--client and --db are resolved here, in one place, so `ingest`/`ask`/`serve`
    can't drift into three slightly different lookup rules."""
    if client is not None:
        clients = load_clients(config_path)
        if client not in clients:
            known = ", ".join(sorted(clients)) or "(none configured)"
            raise ValueError(
                f"no client named '{client}' in {config_path} — known clients: {known}"
            )
        return clients[client]
    return db if db is not None else DEFAULT_DB_PATH
