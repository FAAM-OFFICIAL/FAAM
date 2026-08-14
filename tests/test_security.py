"""Security regression tests.

Each test here corresponds to a real vulnerability that shipped at some point.
Run from the repo root:

    python3 -m unittest tests.test_security -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app.py"


def load_app(home: str):
    os.environ["HOME"] = home
    os.environ["FAAM_DATA_DIR"] = str(Path(home) / ".faam")
    os.environ["OPENAI_API_KEY"] = ""
    spec = importlib.util.spec_from_file_location(f"faamsec_{abs(hash(home))}", APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class PerUserDataTest(unittest.TestCase):
    """One user must never be served another user's holdings.

    These stores were written for a single-user desktop app and kept ONE
    watchlist and ONE portfolio for everybody. Hosted, that served real users
    each other's positions.
    """

    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="faam-sec-")
        (Path(cls.home) / ".faam").mkdir(parents=True, exist_ok=True)
        cls.m = load_app(cls.home)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, ignore_errors=True)

    def test_watchlists_are_isolated(self):
        self.m.save_watchlist(["NVDA", "COIN"], "alice")
        self.assertIn("COIN", self.m.load_watchlist("alice"))
        self.assertNotIn("COIN", self.m.load_watchlist("bob"))

    def test_portfolios_are_isolated(self):
        self.m.save_portfolio([{"id": "1", "symbol": "NVDA", "shares": 500}], "alice")
        self.assertEqual(self.m.load_portfolio("bob"), [])
        self.assertEqual(len(self.m.load_portfolio("alice")), 1)

    def test_broker_choice_is_isolated(self):
        self.m.save_broker({"broker": "schwab"}, "alice")
        self.assertEqual(self.m.load_broker("bob"), {})
        self.assertEqual(self.m.load_broker("alice")["broker"], "schwab")

    def test_a_new_user_gets_defaults_not_someone_elses_list(self):
        self.m.save_watchlist(["PLTR"], "alice")
        fresh = self.m.load_watchlist("brand-new-user")
        self.assertEqual(fresh, list(self.m.DEFAULT_TICKERS))

    def test_writes_do_not_clobber_other_users(self):
        self.m.save_watchlist(["AAA"], "u1")
        self.m.save_watchlist(["BBB"], "u2")
        self.assertEqual(self.m.load_watchlist("u1"), ["AAA"])
        self.assertEqual(self.m.load_watchlist("u2"), ["BBB"])

    def test_concurrent_writes_from_different_users_all_survive(self):
        names = [f"c{i}" for i in range(8)]
        start = threading.Barrier(len(names))

        def write(n):
            start.wait()
            self.m.save_watchlist([n.upper()], n)

        ts = [threading.Thread(target=write, args=(n,)) for n in names]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        for n in names:
            self.assertEqual(self.m.load_watchlist(n), [n.upper()], f"{n} lost its write")


class LegacyMigrationTest(unittest.TestCase):
    """A pre-migration flat file must not be lost when isolation is introduced."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="faam-mig-")
        (Path(self.home) / ".faam").mkdir(parents=True, exist_ok=True)
        with open(Path(self.home) / ".faam" / "watchlist.json", "w") as fh:
            json.dump(["NVDA", "TSLA"], fh)
        self.m = load_app(self.home)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_existing_data_follows_the_first_account(self):
        self.assertEqual(self.m.load_watchlist("owner"), ["NVDA", "TSLA"])

    def test_others_do_not_inherit_it(self):
        self.m.load_watchlist("owner")                # trigger the migration
        self.assertEqual(self.m.load_watchlist("someone-else"),
                         list(self.m.DEFAULT_TICKERS))

    def test_file_is_rewritten_in_the_per_user_shape(self):
        self.m.load_watchlist("owner")
        with open(Path(self.home) / ".faam" / "watchlist.json") as fh:
            raw = json.load(fh)
        self.assertTrue(raw.get("__perUser__"))
        self.assertIn("owner", raw["users"])


class EndpointAuthTest(unittest.TestCase):
    """Endpoints that write server state must require the right caller."""

    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="faam-auth-")
        (Path(cls.home) / ".faam").mkdir(parents=True, exist_ok=True)
        cls.m = load_app(cls.home)
        cls.port = 8807
        users = cls.m.load_users()
        users["alice"] = {"pw": cls.m.hash_password("x"), "tier": 2,
                          "plan": "pro", "email": "a@x.com", "created": 0}
        users["root"] = {"pw": cls.m.hash_password("x"), "tier": 2, "plan": "pro",
                         "email": "r@x.com", "created": 0, "admin": True}
        cls.m.save_users(users)
        cls.alice = cls.m.make_session("alice")
        cls.admin = cls.m.make_session("root")
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.httpd = cls.m.ThreadingHTTPServer(("127.0.0.1", cls.port), cls.m.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.home, ignore_errors=True)

    def post(self, path, body, session=None):
        headers = {"content-type": "application/json", "Origin": self.base}
        if session:
            headers["Cookie"] = f"faam_session={session}"
        req = urllib.request.Request(self.base + path, headers=headers)
        try:
            return urllib.request.urlopen(req, json.dumps(body).encode(), timeout=10).status
        except urllib.error.HTTPError as e:
            return e.code

    def test_stripe_key_write_is_admin_only(self):
        """Regression: this endpoint had no auth check, so anyone on the network
        could replace the server's Stripe secret key."""
        self.assertEqual(self.post("/api/stripe/key", {"key": "sk_evil"}), 404)
        self.assertEqual(self.post("/api/stripe/key", {"key": "sk_evil"}, self.alice), 404)
        self.assertEqual(self.post("/api/stripe/key", {"key": "sk_test_ok"}, self.admin), 200)

    def test_data_writes_require_a_session(self):
        for path, body in (
            ("/api/watchlist/add", {"symbol": "NVDA"}),
            ("/api/watchlist/remove", {"symbol": "NVDA"}),
            ("/api/portfolio/add", {"symbol": "NVDA", "shares": 1, "cost": 1}),
            ("/api/portfolio/remove", {"id": "1"}),
            ("/api/broker", {"broker": "x"}),
        ):
            with self.subTest(path=path):
                self.assertEqual(self.post(path, body), 401)

    def test_signed_in_writes_still_work(self):
        self.assertEqual(self.post("/api/watchlist/add", {"symbol": "NVDA"}, self.alice), 200)
        self.assertEqual(self.post("/api/broker", {"broker": "schwab"}, self.alice), 200)

    def test_host_header_is_validated(self):
        """Anti-DNS-rebinding: a foreign Host must be refused."""
        req = urllib.request.Request(self.base + "/api/health", headers={"Host": "evil.example"})
        try:
            code = urllib.request.urlopen(req, timeout=10).status
        except urllib.error.HTTPError as e:
            code = e.code
        self.assertEqual(code, 421)

    def test_cross_site_origin_is_refused(self):
        headers = {"content-type": "application/json", "Origin": "https://evil.example",
                   "Cookie": f"faam_session={self.alice}"}
        req = urllib.request.Request(self.base + "/api/watchlist/add", headers=headers)
        try:
            code = urllib.request.urlopen(req, json.dumps({"symbol": "X"}).encode(), timeout=10).status
        except urllib.error.HTTPError as e:
            code = e.code
        self.assertEqual(code, 403)


class SessionCookieTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="faam-cookie-")
        (Path(cls.home) / ".faam").mkdir(parents=True, exist_ok=True)
        cls.m = load_app(cls.home)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, ignore_errors=True)

    def test_cookie_is_httponly_and_samesite(self):
        cookie = self.m.Handler._session_cookie(self.m.Handler, "tok")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_session_token_is_rejected_when_tampered(self):
        tok = self.m.make_session("alice")
        self.assertEqual(self.m.read_session(tok), "alice")
        bad = tok[:-4] + ("aaaa" if not tok.endswith("aaaa") else "bbbb")
        self.assertFalse(self.m.read_session(bad))


if __name__ == "__main__":
    unittest.main(verbosity=2)
