"""Tests for the FAAM Stock Paper — FAAM's announcement board.

Run from the repo root:

    python3 -m unittest tests.test_stockpaper -v
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
    spec = importlib.util.spec_from_file_location(f"faamsp_{abs(hash(home))}", APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class StockPaperCase(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="faam-sp-")
        (Path(self.home) / ".faam").mkdir(parents=True, exist_ok=True)
        self.m = load_app(self.home)
        self.m.seed_users()                       # creates the "dev" admin
        users = self.m.load_users()
        for n in ("alice", "bob"):
            users[n] = {"pw": self.m.hash_password("x"), "tier": 0, "plan": "",
                        "admin": False, "email": "", "created": 0}
        self.m.save_users(users)
        self.m._SP_RATE.clear()

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def publish(self, **kw):
        args = dict(title="NVDA beats estimates", body="Revenue came in ahead.",
                    kind="Earnings", tickers="NVDA", pinned=False)
        args.update(kw)
        return self.m.stockpaper_publish("dev", **args)


class TestPublishing(StockPaperCase):

    def test_dev_account_can_publish(self):
        r = self.publish()
        self.assertTrue(r["ok"])
        self.assertEqual(len(self.m.stockpaper_list()), 1)

    def test_regular_users_cannot_publish(self):
        r = self.m.stockpaper_publish("alice", "A headline", "A body of text", "Market", "", False)
        self.assertEqual(r["status"], 403)
        self.assertEqual(self.m.stockpaper_list(), [])

    def test_unknown_user_cannot_publish(self):
        r = self.m.stockpaper_publish("nobody", "A headline", "A body of text", "Market", "", False)
        self.assertEqual(r["status"], 403)

    def test_headline_and_body_are_required(self):
        self.assertEqual(self.publish(title="Hi")["status"], 400)
        self.assertEqual(self.publish(body="short")["status"], 400)

    def test_lengths_are_capped(self):
        r = self.publish(title="T" * 500, body="B" * 20000)
        self.assertEqual(len(r["post"]["title"]), self.m.SP_TITLE_MAX)
        self.assertEqual(len(r["post"]["body"]), self.m.SP_BODY_MAX)

    def test_unknown_section_falls_back(self):
        self.assertEqual(self.publish(kind="<script>")["post"]["kind"], "Market")

    def test_tickers_are_sanitised(self):
        r = self.publish(tickers="nvda, $amd, ../etc, <b>, NVDA, aapl")
        self.assertEqual(r["post"]["tickers"], ["NVDA", "AMD", "AAPL"])

    def test_control_characters_are_stripped(self):
        r = self.publish(title="Bad\x00Head\x07line", body="Line one\x1b[31m\nLine two")
        self.assertNotIn("\x00", r["post"]["title"])
        self.assertNotIn("\x1b", r["post"]["body"])
        self.assertIn("\n", r["post"]["body"], "newlines must survive")

    def test_pinned_posts_lead_the_list(self):
        self.publish(title="Older but pinned", pinned=True)
        self.publish(title="Newer regular story")
        self.assertEqual(self.m.stockpaper_list()[0]["title"], "Older but pinned")

    def test_only_dev_can_delete_posts(self):
        pid = self.publish()["post"]["id"]
        self.assertEqual(self.m.stockpaper_delete("alice", pid)["status"], 403)
        self.assertTrue(self.m.stockpaper_delete("dev", pid)["ok"])
        self.assertEqual(self.m.stockpaper_list(), [])

    def test_list_shows_excerpts_story_shows_everything(self):
        pid = self.publish(body="x" * 1000)["post"]["id"]
        self.assertLess(len(self.m.stockpaper_list()[0]["body"]), 300)
        self.assertEqual(len(self.m.stockpaper_get(pid, "alice")["body"]), 1000)


class TestComments(StockPaperCase):

    def setUp(self):
        super().setUp()
        self.pid = self.publish()["post"]["id"]

    def test_signed_in_users_can_comment(self):
        r = self.m.stockpaper_comment("alice", self.pid, "Nice write-up")
        self.assertTrue(r["ok"])
        self.assertEqual(r["post"]["commentList"][0]["user"], "alice")

    def test_anonymous_cannot_comment(self):
        self.assertEqual(self.m.stockpaper_comment("", self.pid, "hi there")["status"], 401)

    def test_empty_comment_rejected(self):
        self.assertEqual(self.m.stockpaper_comment("alice", self.pid, "   ")["status"], 400)

    def test_comment_on_missing_post(self):
        self.assertEqual(self.m.stockpaper_comment("alice", "nope", "hello")["status"], 404)

    def test_comment_length_capped(self):
        r = self.m.stockpaper_comment("alice", self.pid, "c" * 5000)
        self.assertEqual(len(r["post"]["commentList"][0]["body"]), self.m.SP_COMMENT_MAX)

    def test_rapid_comments_are_rate_limited(self):
        self.m.stockpaper_comment("alice", self.pid, "first")
        r = self.m.stockpaper_comment("alice", self.pid, "second")
        self.assertEqual(r["status"], 429)

    def test_rate_limit_is_per_user(self):
        self.m.stockpaper_comment("alice", self.pid, "first")
        self.assertTrue(self.m.stockpaper_comment("bob", self.pid, "mine")["ok"])

    def test_dev_is_not_rate_limited_and_is_marked_staff(self):
        self.m.stockpaper_comment("dev", self.pid, "one")
        r = self.m.stockpaper_comment("dev", self.pid, "two")
        self.assertTrue(r["ok"])
        self.assertTrue(all(c["staff"] for c in r["post"]["commentList"]))

    def test_authors_delete_own_comments_only(self):
        cid = self.m.stockpaper_comment("alice", self.pid, "mine")["post"]["commentList"][0]["id"]
        self.assertEqual(self.m.stockpaper_uncomment("bob", self.pid, cid)["status"], 403)
        self.assertTrue(self.m.stockpaper_uncomment("alice", self.pid, cid)["ok"])

    def test_dev_can_delete_any_comment(self):
        cid = self.m.stockpaper_comment("alice", self.pid, "spam")["post"]["commentList"][0]["id"]
        self.assertTrue(self.m.stockpaper_uncomment("dev", self.pid, cid)["ok"])

    def test_can_delete_flag_matches_permissions(self):
        self.m.stockpaper_comment("alice", self.pid, "mine")
        self.assertTrue(self.m.stockpaper_get(self.pid, "alice")["commentList"][0]["canDelete"])
        self.assertFalse(self.m.stockpaper_get(self.pid, "bob")["commentList"][0]["canDelete"])
        self.assertTrue(self.m.stockpaper_get(self.pid, "dev")["commentList"][0]["canDelete"])

    def test_concurrent_comments_all_survive(self):
        names = [f"u{i}" for i in range(8)]
        start = threading.Barrier(len(names))

        def go(n):
            start.wait()
            self.m.stockpaper_comment(n, self.pid, f"from {n}")

        ts = [threading.Thread(target=go, args=(n,)) for n in names]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        users = {c["user"] for c in self.m.stockpaper_get(self.pid, "dev")["commentList"]}
        self.assertEqual(users, set(names), "a concurrent comment was lost")


class TestEndpoints(StockPaperCase):
    """The HTTP layer must enforce the same rules — the UI hiding the composer
    is a convenience, not the protection."""

    def setUp(self):
        super().setUp()
        self.port = 8830
        self.base = f"http://127.0.0.1:{self.port}"
        self.httpd = self.m.ThreadingHTTPServer(("127.0.0.1", self.port), self.m.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.dev = self.m.make_session("dev")
        self.alice = self.m.make_session("alice")

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def call(self, path, body=None, session=None):
        h = {"content-type": "application/json", "Origin": self.base}
        if session:
            h["Cookie"] = f"faam_session={session}"
        req = urllib.request.Request(self.base + path, headers=h)
        data = json.dumps(body).encode() if body is not None else None
        try:
            with urllib.request.urlopen(req, data, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_reading_requires_sign_in(self):
        self.assertEqual(self.call("/api/stockpaper")[0], 401)

    def test_regular_user_cannot_publish_over_http(self):
        code, _ = self.call("/api/stockpaper/post",
                            {"title": "Fake news!", "body": "Pretending to be FAAM."}, self.alice)
        self.assertEqual(code, 403)

    def test_full_flow_over_http(self):
        code, r = self.call("/api/stockpaper/post",
                            {"title": "Fed holds rates", "body": "No change this month.",
                             "kind": "Market"}, self.dev)
        self.assertEqual(code, 200)
        pid = r["post"]["id"]
        code, listing = self.call("/api/stockpaper", session=self.alice)
        self.assertEqual(code, 200)
        self.assertFalse(listing["canPost"])
        self.assertEqual(listing["posts"][0]["id"], pid)
        code, _ = self.call("/api/stockpaper/comment", {"id": pid, "body": "Thanks"}, self.alice)
        self.assertEqual(code, 200)
        code, story = self.call(f"/api/stockpaper/post?id={pid}", session=self.alice)
        self.assertEqual(story["commentList"][0]["body"], "Thanks")
        self.assertEqual(self.call("/api/stockpaper/latest", session=self.alice)[1]["latest"]["id"], pid)

    def test_cross_site_post_is_refused(self):
        h = {"content-type": "application/json", "Origin": "https://evil.example",
             "Cookie": f"faam_session={self.dev}"}
        req = urllib.request.Request(self.base + "/api/stockpaper/post", headers=h)
        try:
            urllib.request.urlopen(req, json.dumps({"title": "x" * 10, "body": "y" * 20}).encode(), timeout=10)
            code = 200
        except urllib.error.HTTPError as e:
            code = e.code
            e.close()
        self.assertEqual(code, 403)
        self.assertEqual(self.m.stockpaper_list(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
