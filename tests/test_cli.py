import json
import os

import pytest

from statement_agent.cli import main


def _run(monkeypatch, tmp_path, argv):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["statement-agent"] + argv)
    main()


class TestClientDbMutualExclusion:
    def test_client_and_db_together_is_rejected(self, monkeypatch, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc:
            _run(monkeypatch, tmp_path, ["ask", "hi", "--client", "alice", "--db", "x.db"])
        assert exc.value.code == 2
        assert "pass either --client or --db, not both" in capsys.readouterr().err


class TestClientsSubcommand:
    def test_no_config_file_reports_none_configured(self, monkeypatch, tmp_path, capsys):
        _run(monkeypatch, tmp_path, ["clients"])
        assert "No clients configured" in capsys.readouterr().out

    def test_lists_registered_clients(self, monkeypatch, tmp_path, capsys):
        (tmp_path / "clients.json").write_text(json.dumps({"alice": "ledgers/alice.db"}))
        _run(monkeypatch, tmp_path, ["clients"])
        out = capsys.readouterr().out
        assert "alice -> ledgers/alice.db" in out
        assert "no ledger yet" in out  # ledgers/alice.db doesn't exist in this tmp_path


class TestUnknownClient:
    def test_ask_with_unknown_client_exits_cleanly(self, monkeypatch, tmp_path, capsys):
        (tmp_path / "clients.json").write_text(json.dumps({"alice": "ledgers/alice.db"}))
        with pytest.raises(SystemExit) as exc:
            _run(monkeypatch, tmp_path, ["ask", "hi", "--client", "bob"])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "no client named 'bob'" in err
        assert "alice" in err  # known clients listed to help the user


class TestKnownClientResolvesForIngest:
    def test_ingest_with_client_writes_to_that_clients_db_path(self, monkeypatch, tmp_path, capsys):
        # Deliberately do NOT pre-create tmp_path/ledgers — clients.json routinely
        # points at a path whose parent directory doesn't exist yet (this is
        # exactly the real bug a live smoke test caught: Store used to raise
        # sqlite3.OperationalError: unable to open database file here).
        (tmp_path / "clients.json").write_text(json.dumps({"alice": "ledgers/alice.db"}))
        (tmp_path / "empty_folder").mkdir()
        _run(monkeypatch, tmp_path, ["ingest", "--folder", "empty_folder", "--client", "alice"])
        assert os.path.exists(tmp_path / "ledgers" / "alice.db")
        assert not os.path.exists(tmp_path / "ledger.db")  # never touches the default path
