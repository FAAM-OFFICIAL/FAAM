"""Tests for FAAM Connect: other FAAM apps (the iOS app first) signing in to a
FAAM account and moving the user's data over.

Never touches the network: practice-account prices are patched. Run from the
repo root:

    python3 -m unittest tests.test_connect -v
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import secrets
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app.py"
IOS = "faam-ios"
IOS_REDIRECT = "faam://connected"


def load_app(home: str):
    os.environ["HOME"] = home
    os.environ["FAAM_DATA_DIR"] = str(Path(home) / ".faam")
    os.environ["OPENAI_API_KEY"] = ""
    spec = importlib.util.spec_from_file_location(f"faamcx_{abs(hash(home))}", APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def code_from(redirect: str) -> dict:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(redirect).query))


class ConnectCase(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="faam-cx-")
        (Path(self.home) / ".faam").mkdir(parents=True, exist_ok=True)
        self.m = load_app(self.home)
        self.m.seed_users()
        users = self.m.load_users()
        for name in ("alice", "bob"):
            users[name] = {"pw": self.m.hash_password("x"), "tier": 0, "plan": "",
                           "admin": False, "email": f"{name}@example.com", "created": 0}
        self.m.save_users(users)
        self.m._paper_price = lambda sym: 100.0
        self.m._CONNECT_CODES.clear()
        self.m._CONNECT_RATE.clear()

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def params(self, challenge, **kw):
        p = {"client_id": IOS, "redirect_uri": IOS_REDIRECT, "state": "s123",
             "code_challenge": challenge, "code_challenge_method": "S256",
             "response_type": "code"}
        p.update(kw)
        return p

    def connect(self, user="alice", client=IOS, redirect=IOS_REDIRECT):
        """Run the whole flow; returns the access token."""
        verifier, challenge = pkce()
        r = self.m.connect_approve(user, self.params(challenge, client_id=client,
                                                     redirect_uri=redirect), True)
        self.assertTrue(r.get("ok"), r)
        code = code_from(r["redirect"])["code"]
        t = self.m.connect_token({"grant_type": "authorization_code", "code": code,
                                  "client_id": client, "redirect_uri": redirect,
                                  "code_verifier": verifier})
        self.assertIn("access_token", t, t)
        return t["access_token"]


class TestConsentScreen(ConnectCase):

    def test_ios_app_comes_registered(self):
        _, ch = pkce()
        r = self.m.connect_info("alice", self.params(ch))
        self.assertEqual(r["app"]["name"], "FAAM iOS App")
        self.assertEqual(r["username"], "alice")
        self.assertIn("See and edit your watchlist", r["access"])

    def test_unknown_app_refused(self):
        _, ch = pkce()
        self.assertEqual(self.m.connect_info("alice", self.params(ch, client_id="evil-app"))["status"], 400)

    def test_return_address_must_match_exactly(self):
        _, ch = pkce()
        for bad in ("faam://connected/x", "https://evil.example/cb", "faam://connected?x=1", ""):
            with self.subTest(redirect=bad):
                r = self.m.connect_approve("alice", self.params(ch, redirect_uri=bad), True)
                self.assertEqual(r["status"], 400)
                self.assertNotIn("redirect", r, "never redirect to an unregistered address")

    def test_pkce_is_required(self):
        self.assertEqual(self.m.connect_info("alice", self.params(""))["status"], 400)
        _, ch = pkce()
        self.assertEqual(self.m.connect_info("alice", self.params(ch, code_challenge_method="plain"))["status"], 400)

    def test_non_string_params_are_refused_not_crashing(self):
        r = self.m.connect_approve("alice", {"client_id": IOS, "redirect_uri": IOS_REDIRECT,
                                             "code_challenge": 10 ** 42, "state": None}, True)
        self.assertEqual(r["status"], 400)

    def test_cancel_sends_access_denied_back(self):
        _, ch = pkce()
        r = self.m.connect_approve("alice", self.params(ch), False)
        q = code_from(r["redirect"])
        self.assertEqual((q["error"], q["state"]), ("access_denied", "s123"))
        self.assertNotIn("code", q)

    def test_state_is_echoed(self):
        _, ch = pkce()
        r = self.m.connect_approve("alice", self.params(ch), True)
        self.assertTrue(r["redirect"].startswith(IOS_REDIRECT + "?"))
        self.assertEqual(code_from(r["redirect"])["state"], "s123")


class TestTokenExchange(ConnectCase):

    def setUp(self):
        super().setUp()
        self.verifier, ch = pkce()
        r = self.m.connect_approve("alice", self.params(ch), True)
        self.code = code_from(r["redirect"])["code"]

    def swap(self, **kw):
        body = {"grant_type": "authorization_code", "code": self.code, "client_id": IOS,
                "redirect_uri": IOS_REDIRECT, "code_verifier": self.verifier}
        body.update(kw)
        return self.m.connect_token(body)

    def test_good_exchange(self):
        t = self.swap()
        self.assertEqual((t["token_type"], t["username"]), ("Bearer", "alice"))
        self.assertTrue(t["access_token"].startswith("faam_"))

    def test_code_is_single_use(self):
        self.assertIn("access_token", self.swap())
        self.assertEqual(self.swap()["error"], "invalid_grant")

    def test_wrong_verifier(self):
        self.assertEqual(self.swap(code_verifier=pkce()[0])["error"], "invalid_grant")

    def test_wrong_client_or_redirect(self):
        self.assertEqual(self.swap(redirect_uri="faam://other")["error"], "invalid_grant")

    def test_expired_code(self):
        for g in self.m._CONNECT_CODES.values():
            g["exp"] = time.time() - 1
        self.assertEqual(self.swap()["error"], "invalid_grant")

    def test_wrong_grant_type(self):
        self.assertEqual(self.swap(grant_type="password")["error"], "unsupported_grant_type")

    def test_tokens_are_stored_hashed(self):
        token = self.swap()["access_token"]
        raw = (Path(self.home) / ".faam" / "connect.json").read_text()
        self.assertNotIn(token, raw)
        self.assertIn(hashlib.sha256(token.encode()).hexdigest(), raw)

    def test_rate_limited(self):
        for _ in range(30):
            self.m.connect_token({"grant_type": "authorization_code"}, "9.9.9.9")
        self.assertEqual(self.m.connect_token({}, "9.9.9.9")["status"], 429)


class TestApi(ConnectCase):

    def api(self, token, method, path, body=None):
        who = self.m.connect_token_user(token)
        self.assertIsNotNone(who)
        return self.m.connect_api(method, path, who["username"], body or {})

    def test_data_moves_over(self):
        self.m.save_watchlist(["NVDA", "AAPL"], "alice")
        self.m.save_portfolio([{"id": "abcd1234", "symbol": "NVDA", "shares": 2, "cost": 90}], "alice")
        token = self.connect()
        out = self.api(token, "GET", "/api/v1/export")
        self.assertEqual(out["watchlist"], ["NVDA", "AAPL"])
        self.assertEqual(out["portfolio"][0]["symbol"], "NVDA")
        self.assertEqual(out["practice"]["cash"], self.m.PAPER_START_CASH)
        self.assertIn("xp", out["learn"])

    def test_each_token_sees_only_its_own_account(self):
        self.m.save_watchlist(["TSLA"], "bob")
        self.m.save_watchlist(["NVDA"], "alice")
        self.assertEqual(self.api(self.connect("bob"), "GET", "/api/v1/watchlist")["symbols"], ["TSLA"])
        self.assertEqual(self.api(self.connect("alice"), "GET", "/api/v1/watchlist")["symbols"], ["NVDA"])

    def test_app_can_edit_watchlist(self):
        token = self.connect()
        r = self.api(token, "POST", "/api/v1/watchlist", {"symbols": ["amd", "AMD", "msft"]})
        self.assertEqual(r["symbols"], ["AMD", "MSFT"])
        self.assertEqual(self.m.load_watchlist("alice"), ["AMD", "MSFT"])

    def test_bad_watchlist_rejected(self):
        token = self.connect()
        for bad in ("NVDA", ["<script>"], ["A"] * 101, [{"x": 1}]):
            with self.subTest(bad=str(bad)[:30]):
                self.assertEqual(self.api(token, "POST", "/api/v1/watchlist", {"symbols": bad})["status"], 400)

    def test_app_can_edit_portfolio(self):
        token = self.connect()
        r = self.api(token, "POST", "/api/v1/portfolio",
                     {"positions": [{"symbol": "aapl", "shares": "1.5", "cost": 200}]})
        p = r["positions"][0]
        self.assertEqual((p["symbol"], p["shares"], p["cost"]), ("AAPL", 1.5, 200.0))
        self.assertRegex(p["id"], r"^[0-9a-f]{8}$")

    def test_bad_portfolio_rejected(self):
        token = self.connect()
        for bad in ([{"symbol": "AAPL", "shares": -1, "cost": 1}],
                    [{"symbol": "AAPL", "shares": "nan", "cost": 1}],
                    [{"symbol": "../x", "shares": 1, "cost": 1}], "nope"):
            with self.subTest(bad=str(bad)[:40]):
                self.assertEqual(self.api(token, "POST", "/api/v1/portfolio", {"positions": bad})["status"], 400)

    def test_unknown_route(self):
        self.assertEqual(self.api(self.connect(), "GET", "/api/v1/nope")["status"], 404)

    def test_garbage_tokens(self):
        for bad in ("", "faam_nope", "x" * 500, None):
            self.assertIsNone(self.m.connect_token_user(bad))

    def test_disconnect_revokes(self):
        token = self.connect()
        self.assertEqual([a["name"] for a in self.m.connect_my_apps("alice")], ["FAAM iOS App"])
        self.m.connect_disconnect("alice", IOS)
        self.assertIsNone(self.m.connect_token_user(token))
        self.assertEqual(self.m.connect_my_apps("alice"), [])

    def test_disconnect_is_per_user(self):
        a, b = self.connect("alice"), self.connect("bob")
        self.m.connect_disconnect("alice", IOS)
        self.assertIsNone(self.m.connect_token_user(a))
        self.assertIsNotNone(self.m.connect_token_user(b))

    def test_expired_token(self):
        token = self.connect()
        d = self.m._connect_load()
        for t in d["tokens"].values():
            t["expires"] = int(time.time()) - 1
        self.m._connect_save(d)
        self.assertIsNone(self.m.connect_token_user(token))

    def test_deleted_account_loses_access(self):
        token = self.connect()
        users = self.m.load_users()
        users.pop("alice")
        self.m.save_users(users)
        self.assertIsNone(self.m.connect_token_user(token))

    def test_reconnecting_retires_old_tokens(self):
        tokens = [self.connect() for _ in range(self.m.CONNECT_TOKENS_PER_APP + 2)]
        live = [t for t in tokens if self.m.connect_token_user(t)]
        self.assertEqual(len(live), self.m.CONNECT_TOKENS_PER_APP)
        self.assertIsNotNone(self.m.connect_token_user(tokens[-1]), "the newest must work")


class TestDevApps(ConnectCase):

    def test_only_dev_sees_or_changes_apps(self):
        self.assertEqual(self.m.connect_dev_apps("alice")["status"], 404)
        self.assertEqual(self.m.connect_dev_create("alice", "X", "x://y")["status"], 404)
        self.assertEqual(self.m.connect_dev_delete("alice", IOS)["status"], 404)
        self.assertIsNotNone(self.m.connect_token_user(self.connect()), "nothing changed")

    def test_dev_registers_an_app_that_can_then_connect(self):
        r = self.m.connect_dev_create("dev", "FAAM iPad App", "faamipad://connected")
        cid = r["created"]
        self.assertRegex(cid, r"^faam-ipad-app-[0-9a-f]{6}$")
        token = self.connect("alice", client=cid, redirect="faamipad://connected")
        self.assertEqual(self.m.connect_token_user(token)["app"], "FAAM iPad App")

    def test_dangerous_return_links_refused(self):
        for bad in ("javascript:alert(1)", "data:text/html,x", "http://evil.example/cb",
                    "file:///etc/passwd", "no-scheme", "faam://a b", "https://x/#frag"):
            with self.subTest(uri=bad):
                self.assertEqual(self.m.connect_dev_create("dev", "Bad", bad)["status"], 400)

    def test_allowed_return_links(self):
        for good in ("faam://connected", "com.faam.ios:/oauth", "https://faam.app/cb",
                     "http://localhost:3000/cb"):
            with self.subTest(uri=good):
                self.assertTrue(self.m.connect_redirect_ok(good))

    def test_removing_an_app_signs_everyone_out(self):
        token = self.connect()
        self.m.connect_dev_delete("dev", IOS)
        self.assertIsNone(self.m.connect_token_user(token))
        _, ch = pkce()
        self.assertEqual(self.m.connect_info("alice", self.params(ch))["status"], 400)

    def test_user_counts(self):
        self.connect("alice")
        self.connect("bob")
        ios = [a for a in self.m.connect_dev_apps("dev")["apps"] if a["clientId"] == IOS][0]
        self.assertEqual(ios["users"], 2)


class TestHttp(ConnectCase):

    def setUp(self):
        super().setUp()
        self.port = 8832
        self.base = f"http://127.0.0.1:{self.port}"
        self.httpd = self.m.ThreadingHTTPServer(("127.0.0.1", self.port), self.m.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.alice = self.m.make_session("alice")

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def call(self, method, path, body=None, cookie=None, bearer=None, origin=None, form=False):
        h = {}
        data = None
        if body is not None:
            if form:
                data = urllib.parse.urlencode(body).encode()
                h["content-type"] = "application/x-www-form-urlencoded"
            else:
                data = json.dumps(body).encode()
                h["content-type"] = "application/json"
        if cookie:
            h["Cookie"] = f"faam_session={cookie}"
        if bearer:
            h["Authorization"] = f"Bearer {bearer}"
        if origin:
            h["Origin"] = origin
        req = urllib.request.Request(self.base + path, data=data, headers=h, method=method)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(req, timeout=10) as r:
                raw = r.read()
                return r.status, r.headers, raw
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers, e.read()

    def test_signed_out_visitors_go_to_sign_in_then_back(self):
        code, h, _ = self.call("GET", "/connect?client_id=faam-ios&state=1")
        self.assertEqual(code, 302)
        self.assertEqual(h["Location"], "/login?next=" + urllib.parse.quote(
            "/connect?client_id=faam-ios&state=1", safe=""))

    def test_signed_in_see_the_page(self):
        code, h, raw = self.call("GET", "/connect", cookie=self.alice)
        self.assertEqual(code, 200)
        self.assertIn(b"Connect to", raw)

    def test_full_flow_over_http(self):
        verifier, ch = pkce()
        q = urllib.parse.urlencode({"client_id": IOS, "redirect_uri": IOS_REDIRECT,
                                    "state": "abc", "code_challenge": ch,
                                    "code_challenge_method": "S256"})
        code, _, raw = self.call("GET", "/api/connect/info?" + q, cookie=self.alice)
        self.assertEqual((code, json.loads(raw)["app"]["name"]), (200, "FAAM iOS App"))
        code, _, raw = self.call("POST", "/api/connect/approve",
                                 {**dict(urllib.parse.parse_qsl(q)), "approve": True},
                                 cookie=self.alice, origin=self.base)
        self.assertEqual(code, 200)
        grant = code_from(json.loads(raw)["redirect"])
        # Form-encoded, the way AppAuth and most OAuth libraries send it.
        code, _, raw = self.call("POST", "/api/connect/token", {
            "grant_type": "authorization_code", "code": grant["code"], "client_id": IOS,
            "redirect_uri": IOS_REDIRECT, "code_verifier": verifier}, form=True)
        self.assertEqual(code, 200, raw)
        token = json.loads(raw)["access_token"]
        code, _, raw = self.call("GET", "/api/v1/me", bearer=token)
        self.assertEqual((code, json.loads(raw)["username"]), (200, "alice"))
        code, _, raw = self.call("POST", "/api/v1/watchlist", {"symbols": ["NVDA"]}, bearer=token)
        self.assertEqual(json.loads(raw)["symbols"], ["NVDA"])

    def test_v1_ignores_browser_cookies(self):
        code, _, _ = self.call("GET", "/api/v1/me", cookie=self.alice)
        self.assertEqual(code, 401)

    def test_approve_needs_sign_in_and_same_site(self):
        _, ch = pkce()
        body = {"client_id": IOS, "redirect_uri": IOS_REDIRECT, "code_challenge": ch, "approve": True}
        self.assertEqual(self.call("POST", "/api/connect/approve", body)[0], 401)
        self.assertEqual(self.call("POST", "/api/connect/approve", body, cookie=self.alice,
                                   origin="https://evil.example")[0], 403)

    def test_dev_routes_hidden(self):
        code, _, raw = self.call("GET", "/api/connect/apps", cookie=self.alice)
        self.assertNotIn("dev", json.loads(raw))
        self.assertEqual(self.call("POST", "/api/connect/dev/create",
                                   {"name": "X", "redirectUri": "x://y"},
                                   cookie=self.alice, origin=self.base)[0], 404)
        code, _, raw = self.call("GET", "/api/connect/apps", cookie=self.m.make_session("dev"))
        self.assertIn(IOS, [a["clientId"] for a in json.loads(raw)["dev"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
