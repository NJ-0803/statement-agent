import json
import os

import pytest

from statement_agent.clients import load_clients, resolve_db_path


@pytest.fixture
def config_path(tmp_path):
    return str(tmp_path / "clients.json")


class TestLoadClients:
    def test_missing_file_returns_empty_dict(self, config_path):
        assert load_clients(config_path) == {}

    def test_valid_config_parses(self, config_path):
        with open(config_path, "w") as f:
            json.dump({"alice": "ledgers/alice.db", "bob": "ledgers/bob.db"}, f)
        assert load_clients(config_path) == {"alice": "ledgers/alice.db", "bob": "ledgers/bob.db"}

    def test_non_dict_config_raises(self, config_path):
        with open(config_path, "w") as f:
            json.dump(["alice", "bob"], f)
        with pytest.raises(ValueError, match="must be a JSON object"):
            load_clients(config_path)

    def test_non_string_value_raises(self, config_path):
        with open(config_path, "w") as f:
            json.dump({"alice": 123}, f)
        with pytest.raises(ValueError, match="must be a JSON object"):
            load_clients(config_path)


class TestResolveDbPath:
    def test_no_client_no_db_falls_back_to_default(self, config_path):
        assert resolve_db_path(client=None, db=None, config_path=config_path) == "ledger.db"

    def test_no_client_returns_given_db_unchanged(self, config_path):
        assert resolve_db_path(client=None, db="my_ledger.db", config_path=config_path) == "my_ledger.db"

    def test_known_client_resolves_to_its_db_path(self, config_path):
        with open(config_path, "w") as f:
            json.dump({"alice": "ledgers/alice.db"}, f)
        assert resolve_db_path(client="alice", db=None, config_path=config_path) == "ledgers/alice.db"

    def test_unknown_client_raises_with_known_clients_listed(self, config_path):
        with open(config_path, "w") as f:
            json.dump({"alice": "ledgers/alice.db"}, f)
        with pytest.raises(ValueError, match="no client named 'bob'.*alice"):
            resolve_db_path(client="bob", db=None, config_path=config_path)

    def test_client_requested_but_no_config_file_raises(self, config_path):
        assert not os.path.exists(config_path)
        with pytest.raises(ValueError, match="none configured"):
            resolve_db_path(client="alice", db=None, config_path=config_path)
