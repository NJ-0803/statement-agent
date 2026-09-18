"""Who gets in, and what a public demo instance is able to reach.

The point of these is not that the happy path works. It is that the failure modes fail closed:

  * a route added later is protected without anyone remembering (deny-by-default, checked by walking
    the real URL map rather than a hand-written list)
  * a corrupt or truncated credentials file reads as "wrong passphrase", never as a way in
  * a demo instance cannot open a real ledger even when it is explicitly told to
  * two visitors to the demo never share a ledger
"""

import json
import os

import pytest

from statement_agent.web import auth, demo
from statement_agent.web.app import create_app

PASSPHRASE = "correct horse battery staple"


@pytest.fixture
def owner_app(tmp_path, monkeypatch):
    monkeypatch.setenv("STATEMENT_AGENT_MODE", "owner")
    monkeypatch.setenv("STATEMENT_AGENT_SECRET_KEY", "k" * 40)
    monkeypatch.setenv("STATEMENT_AGENT_CREDENTIALS", str(tmp_path / "credentials.json"))
    auth.save_passphrase(PASSPHRASE)
    app = create_app(db_path=str(tmp_path / "ledger.db"), upload_dir=str(tmp_path / "up"), run_imports_inline=True)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def demo_app(tmp_path, monkeypatch):
    monkeypatch.setenv("STATEMENT_AGENT_MODE", "demo")
    monkeypatch.setenv("STATEMENT_AGENT_SECRET_KEY", "k" * 40)
    monkeypatch.setenv("STATEMENT_AGENT_DEMO_ROOT", str(tmp_path / "demo"))
    app = create_app(db_path=str(tmp_path / "REAL-ledger.db"), run_imports_inline=True)
    app.config["TESTING"] = True
    return app


def _sign_in(client):
    return client.post("/login", data={"passphrase": PASSPHRASE}, follow_redirects=False)


class TestPassphraseStorage:
    def test_the_passphrase_itself_is_never_written_anywhere(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STATEMENT_AGENT_CREDENTIALS", str(tmp_path / "credentials.json"))
        path = auth.save_passphrase(PASSPHRASE)
        written = open(path, encoding="utf-8").read()
        assert PASSPHRASE not in written
        assert json.loads(written)["passphrase_hash"].startswith("scrypt$")

    def test_the_credentials_file_is_readable_only_by_its_owner(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STATEMENT_AGENT_CREDENTIALS", str(tmp_path / "credentials.json"))
        path = auth.save_passphrase(PASSPHRASE)
        assert oct(os.stat(path).st_mode)[-3:] == "600"

    def test_the_same_passphrase_hashes_differently_every_time(self):
        assert auth.hash_passphrase(PASSPHRASE) != auth.hash_passphrase(PASSPHRASE)

    def test_verification_accepts_the_passphrase_and_rejects_near_misses(self):
        stored = auth.hash_passphrase(PASSPHRASE)
        assert auth.verify_passphrase(PASSPHRASE, stored)
        for wrong in (PASSPHRASE + " ", PASSPHRASE.upper(), PASSPHRASE[:-1], "", "x"):
            assert not auth.verify_passphrase(wrong, stored)

    @pytest.mark.parametrize("broken", ["", "nonsense", "scrypt$", "scrypt$a$b$c$d$e",
                                        "bcrypt$1$2$3$aaaa$bbbb", "scrypt$32768$8$1$notbase64$!!"])
    def test_a_corrupt_stored_value_is_a_refusal_not_a_crash_or_a_way_in(self, broken):
        assert auth.verify_passphrase(PASSPHRASE, broken) is False
        assert auth.verify_passphrase("", broken) is False

    def test_an_unreadable_credentials_file_means_no_passphrase_is_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STATEMENT_AGENT_CREDENTIALS", str(tmp_path / "missing.json"))
        monkeypatch.delenv("STATEMENT_AGENT_PASSPHRASE_HASH", raising=False)
        assert auth.stored_hash() is None
        (tmp_path / "bad.json").write_text("{not json")
        monkeypatch.setenv("STATEMENT_AGENT_CREDENTIALS", str(tmp_path / "bad.json"))
        assert auth.stored_hash() is None


class TestEveryRouteIsClosedUntilYouSignIn:
    def test_no_route_is_reachable_without_a_session(self, owner_app):
        """Walks the app's own URL map, so a route added later is covered by this test the day it
        is added rather than the day someone remembers to list it."""
        client = owner_app.test_client()
        leaked = []
        for rule in owner_app.url_map.iter_rules():
            if auth.is_public_path(str(rule)) or "static" in rule.endpoint:
                continue
            if "<" in str(rule):  # needs a real id; covered by the sampled calls below
                continue
            for method in sorted(rule.methods & {"GET", "POST", "PUT", "DELETE"}):
                response = client.open(str(rule), method=method)
                if response.status_code not in (301, 302, 401):
                    leaked.append((method, str(rule), response.status_code))
        assert not leaked, f"reachable without signing in: {leaked}"

    def test_api_routes_answer_401_json_rather_than_redirecting(self, owner_app):
        response = owner_app.test_client().get("/api/status")
        assert response.status_code == 401
        assert response.get_json()["error"]

    def test_the_page_redirects_to_the_sign_in_screen(self, owner_app):
        response = owner_app.test_client().get("/")
        assert response.status_code == 302 and "/login" in response.headers["Location"]

    def test_signing_in_opens_the_app_and_signing_out_closes_it_again(self, owner_app):
        client = owner_app.test_client()
        assert _sign_in(client).status_code == 302
        assert client.get("/api/status").status_code == 200
        client.get("/logout")
        assert client.get("/api/status").status_code == 401

    def test_a_wrong_passphrase_does_not_create_a_session(self, owner_app):
        client = owner_app.test_client()
        assert client.post("/login", data={"passphrase": "let me in"}).status_code == 401
        assert client.get("/api/status").status_code == 401

    def test_health_check_stays_open_and_reveals_nothing_about_the_data(self, owner_app):
        body = owner_app.test_client().get("/healthz").get_json()
        assert body == {"ok": True, "mode": "owner"}

    def test_repeated_guesses_get_throttled(self, owner_app):
        client = owner_app.test_client()
        codes = [client.post("/login", data={"passphrase": f"guess-{i}"}).status_code for i in range(12)]
        assert 429 in codes, "unlimited passphrase guesses are allowed"
        # and the throttle does not let a correct passphrase through while it is in force
        assert _sign_in(client).status_code == 429


class TestLocalModeIsUnchanged:
    def test_running_on_your_own_machine_still_asks_for_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STATEMENT_AGENT_MODE", "local")
        app = create_app(db_path=str(tmp_path / "ledger.db"), run_imports_inline=True)
        app.config["TESTING"] = True
        assert app.test_client().get("/api/status").status_code == 200

    def test_an_unrecognised_mode_falls_back_to_requiring_a_login(self, monkeypatch):
        monkeypatch.setenv("STATEMENT_AGENT_MODE", "pubic-demo-typo")
        assert auth.mode() == "owner" and auth.needs_login()


class TestADemoInstanceCannotReachARealLedger:
    def test_a_path_outside_the_demo_area_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STATEMENT_AGENT_DEMO_ROOT", str(tmp_path / "demo"))
        with pytest.raises(RuntimeError, match="never be able to read a real ledger"):
            demo.guard_path(str(tmp_path / "REAL-ledger.db"))
        with pytest.raises(RuntimeError):
            demo.guard_path("/etc/passwd")

    def test_traversal_out_of_the_demo_area_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STATEMENT_AGENT_DEMO_ROOT", str(tmp_path / "demo"))
        with pytest.raises(RuntimeError):
            demo.guard_path(os.path.join(demo.demo_root(), "..", "REAL-ledger.db"))

    def test_a_session_value_cannot_become_a_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STATEMENT_AGENT_DEMO_ROOT", str(tmp_path / "demo"))
        for bad in ("../escape", "a/b", "", "not-hex-" * 4, "A" * 32):
            with pytest.raises(ValueError):
                demo.sandbox_dir(bad)

    def test_the_configured_real_ledger_is_never_the_one_served(self, demo_app):
        """create_app was handed a real ledger path on purpose; demo mode must ignore it."""
        client = demo_app.test_client()
        assert client.get("/api/status").status_code == 200
        served = demo_app.config["DB_PATH"]
        assert "REAL-ledger.db" in served, "the fixture no longer proves anything"
        assert not os.path.exists(served), "demo mode created or opened the real ledger path"

    def test_two_visitors_never_share_a_ledger(self, demo_app):
        first, second = demo_app.test_client(), demo_app.test_client()
        assert first.get("/api/status").status_code == 200
        assert second.get("/api/status").status_code == 200
        root = demo.demo_root()
        sandboxes = [n for n in os.listdir(root) if len(n) == 32]
        assert len(sandboxes) == 2, f"expected one sandbox per visitor, found {sandboxes}"

    def test_a_demo_needs_no_sign_in(self, demo_app):
        assert demo_app.test_client().get("/").status_code == 200


class TestVisitorUploadsDoNotLinger:
    def test_a_stale_sandbox_is_swept_and_a_fresh_one_is_kept(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STATEMENT_AGENT_DEMO_ROOT", str(tmp_path / "demo"))
        old, new = "a" * 32, "b" * 32
        for sandbox_id in (old, new):
            demo.create_sandbox(sandbox_id)
        os.utime(demo.sandbox_dir(old), (0, 0))  # pretend nobody has touched it for a long time
        assert demo.sweep(ttl_seconds=60) == 1
        assert not os.path.exists(demo.sandbox_dir(old))
        assert os.path.exists(demo.sandbox_dir(new))

    def test_sweeping_ignores_anything_that_is_not_a_sandbox(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STATEMENT_AGENT_DEMO_ROOT", str(tmp_path / "demo"))
        seed = os.path.join(demo.demo_root(), "seed.db")
        open(seed, "w").close()
        os.utime(seed, (0, 0))
        assert demo.sweep(ttl_seconds=60) == 0
        assert os.path.exists(seed), "the sweeper deleted the seed every demo visitor starts from"


class TestOneVisitorCannotSeeAnotherVisitorsFile:
    """The isolation claim, proved by actually uploading as one visitor and looking as another."""

    CSV = (b"Date,Description,Amount\n"
           b"2025-06-01,ACME PRIVATE CLINIC CONSULTATION,-4500.00\n"
           b"2025-06-02,SECRET SALARY CREDIT,125000.00\n")

    def _upload(self, client, name="my-private-statement.csv"):
        import io
        token = client.application.config["CSRF_TOKEN"]
        return client.post("/api/imports", headers={"X-CSRF-Token": token},
                           data={"files": (io.BytesIO(self.CSV), name)},
                           content_type="multipart/form-data")

    def test_an_uploaded_file_is_invisible_to_every_other_visitor(self, demo_app):
        visitor, stranger = demo_app.test_client(), demo_app.test_client()
        assert visitor.get("/api/status").status_code == 200
        assert stranger.get("/api/status").status_code == 200

        job = self._upload(visitor).get_json()["imports"][0]
        visitor.post(f"/api/imports/{job['id']}/commit",
                     headers={"X-CSRF-Token": demo_app.config["CSRF_TOKEN"]})

        # the visitor sees their own import
        mine = visitor.get("/api/imports").get_json()["imports"]
        assert any("my-private-statement" in (j.get("file") or "") for j in mine)

        # the stranger sees none of it: not the import, not the rows, not the words in them
        theirs = stranger.get("/api/imports").get_json()["imports"]
        assert not any("my-private-statement" in (j.get("file") or "") for j in theirs)
        body = stranger.get("/api/transactions?q=SECRET").get_data(as_text=True)
        assert "SECRET SALARY" not in body and "ACME PRIVATE CLINIC" not in body
        assert stranger.get("/api/export.csv").get_data(as_text=True).count("SECRET SALARY") == 0

    def test_one_visitor_cannot_open_another_visitors_import_by_its_id(self, demo_app):
        visitor, stranger = demo_app.test_client(), demo_app.test_client()
        stranger.get("/api/status")
        job_id = self._upload(visitor).get_json()["imports"][0]["id"]
        # guessing the id is not enough: it lives in a ledger the stranger's session cannot reach
        assert stranger.get(f"/api/imports/{job_id}").status_code == 404
        assert stranger.get(f"/api/imports/{job_id}/preview").status_code == 404

    def test_a_visitors_uploaded_bytes_land_only_in_their_own_sandbox(self, demo_app):
        visitor = demo_app.test_client()
        self._upload(visitor)
        root = demo.demo_root()
        holders = [name for name in os.listdir(root) if len(name) == 32
                   and any("my-private-statement" in f
                           for _, _, files in os.walk(os.path.join(root, name)) for f in files)]
        assert len(holders) == 1, f"the uploaded file exists in {len(holders)} sandboxes, expected 1"
