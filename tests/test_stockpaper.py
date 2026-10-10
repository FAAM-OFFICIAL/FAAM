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



class TestAiDesk(StockPaperCase):
    """AI drafting and polishing. The model and image calls are mocked, so these
    run offline and cost nothing."""

    def setUp(self):
        super().setUp()
        self.m.OPENAI_API_KEY = "test-key"
        self.sent = []
        self.reply = {"title": "Chips rally", "body": "AMD rose. NVDA rose too.",
                      "kind": "Market", "tickers": ["AMD"], "notes": ["Fixed grammar"]}

        def fake_chat(messages, system=None):
            self.sent.append((system, messages[-1]["content"]))
            return {"choices": [{"message": {"content": json.dumps(self.reply)}}],
                    "usage": {}, "model": "gpt-4.1-mini"}

        self.m.openai_chat = fake_chat
        self.m.extract_text = lambda r: r["choices"][0]["message"]["content"]
        self.m._openai_image = lambda prompt: (b"\xff\xd8fakejpeg", "")
        self.m.yahoo_quote = lambda sym, range_="1mo", interval="1d": {
            "name": f"{sym} Corp", "history": [{"c": 100.0}, {"c": 110.0}]}
        self.m.fetch_news = lambda sym, limit=5: [{"title": f"{sym} beats estimates", "publisher": "Wire"}]
        self.m._SP_AI_RATE.clear()

    def test_ticker_extraction_skips_common_acronyms(self):
        self.assertEqual(self.m.sp_tickers_in("$nvda and AMD after the CPI, AI and the FED"),
                         ["NVDA", "AMD"])

    def test_draft_is_dev_only(self):
        self.assertEqual(self.m.stockpaper_ai_draft("alice", "write a story")["status"], 404)
        self.assertEqual(self.sent, [], "no AI call should happen for a non-dev")

    def test_draft_is_grounded_in_live_facts(self):
        r = self.m.stockpaper_ai_draft("dev", "a note on $AMD")
        self.assertTrue(r["ok"])
        self.assertTrue(r["grounded"])
        self.assertEqual(r["sources"], ["AMD"])
        prompt = self.sent[-1][1]
        self.assertIn("$110.00", prompt, "the live price must reach the model")
        self.assertIn("AMD beats estimates", prompt, "headlines must reach the model")
        self.assertIn("never invent numbers", self.sent[-1][0])

    def test_draft_without_data_tells_the_model_not_to_state_figures(self):
        self.m.stockpaper_ai_draft("dev", "a general note about markets")
        self.assertIn("Do not state any prices", self.sent[-1][1])

    def test_draft_never_publishes(self):
        self.m.stockpaper_ai_draft("dev", "a note on $AMD")
        self.assertEqual(self.m.stockpaper_list(), [])

    def test_no_key_is_reported(self):
        self.m.OPENAI_API_KEY = ""
        self.assertEqual(self.m.stockpaper_ai_draft("dev", "a note on $AMD")["status"], 503)

    def test_bad_model_output_is_handled(self):
        self.m.openai_chat = lambda m, system=None: {"choices": [{"message": {"content": "not json"}}]}
        self.assertEqual(self.m.stockpaper_ai_draft("dev", "a note on $AMD")["status"], 502)

    def test_polish_requires_an_option_and_text(self):
        self.assertEqual(self.m.stockpaper_ai_polish("dev", "Headline", "A story body here",
                                                     False, False, False)["status"], 400)
        self.assertEqual(self.m.stockpaper_ai_polish("dev", "Hi", "x", True)["status"], 400)

    def test_polish_is_dev_only(self):
        self.assertEqual(self.m.stockpaper_ai_polish("alice", "Headline", "A story body here",
                                                     True)["status"], 404)

    def test_grammar_and_developing_instructions_reach_the_model(self):
        r = self.m.stockpaper_ai_polish("dev", "Headline here", "A story body here",
                                        grammar=True, developing=True)
        self.assertTrue(r["developing"])
        instr = self.sent[-1][1]
        self.assertIn("Fix grammar", instr)
        self.assertIn("What we know so far", instr)
        self.assertIn("Never add facts", self.sent[-1][0])

    def test_picture_only_skips_the_text_model(self):
        r = self.m.stockpaper_ai_polish("dev", "Headline here", "A story body here", False, False, True)
        self.assertEqual(self.sent, [])
        self.assertTrue(r["image"])
        self.assertEqual(r["imageCaption"], "Illustration generated by AI")

    def test_picture_round_trip_and_cleanup(self):
        img = self.m.stockpaper_ai_polish("dev", "Headline here", "A story body here",
                                          False, False, True)["image"]
        path = self.m._sp_image_path(img)
        self.assertTrue(path.exists())
        post = self.m.stockpaper_publish("dev", "Headline here", "A story body here",
                                         "Market", "", False, True, img)["post"]
        self.assertEqual(post["image"], img)
        self.assertTrue(post["developing"])
        self.m.stockpaper_delete("dev", post["id"])
        self.assertFalse(path.exists(), "deleting a story must delete its picture")

    def test_failed_picture_still_returns_the_text(self):
        self.m._openai_image = lambda prompt: (b"", "quota exceeded")
        r = self.m.stockpaper_ai_polish("dev", "Headline here", "A story body here", True, False, True)
        self.assertTrue(r["ok"])
        self.assertEqual(r["image"], "")
        self.assertTrue(any("Picture failed" in n for n in r["notes"]))

    def test_publish_refuses_pictures_it_did_not_make(self):
        for bad in ("0123456789abcdef", "../../etc/passwd", "short"):
            with self.subTest(image=bad):
                r = self.m.stockpaper_publish("dev", "Headline", "A story body", "Market", "",
                                              False, False, bad)
                self.assertEqual(r["status"], 400)

    def test_image_ids_cannot_escape_the_folder(self):
        for bad in ("../../etc/passwd", "..%2f..", "abc", "0123456789ABCDEF", ""):
            self.assertIsNone(self.m._sp_image_path(bad))

    def test_ai_is_rate_limited(self):
        self.m._SP_AI_RATE[:] = [__import__("time").time()] * self.m.SP_AI_HOURLY
        self.assertEqual(self.m.stockpaper_ai_draft("dev", "a note on $AMD")["status"], 429)


class TestAiEndpoints(TestEndpoints):

    def setUp(self):
        super().setUp()
        self.m.OPENAI_API_KEY = "test-key"
        self.m._openai_image = lambda prompt: (b"\xff\xd8fakejpeg", "")
        self.m._SP_AI_RATE.clear()

    def test_ai_routes_are_dev_only(self):
        for path, body in (("/api/stockpaper/ai/draft", {"prompt": "a note on $AMD"}),
                           ("/api/stockpaper/ai/polish", {"title": "Headline", "body": "A body", "picture": True})):
            with self.subTest(path=path):
                self.assertEqual(self.call(path, body, self.alice)[0], 404)

    def test_images_need_sign_in_and_valid_ids(self):
        code, r = self.call("/api/stockpaper/ai/polish",
                            {"title": "Headline here", "body": "A story body here", "picture": True}, self.dev)
        self.assertEqual(code, 200)
        img = r["image"]
        req = urllib.request.Request(f"{self.base}/api/stockpaper/img?id={img}")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(cm.exception.code, 401)
        cm.exception.close()
        req = urllib.request.Request(f"{self.base}/api/stockpaper/img?id={img}",
                                     headers={"Cookie": f"faam_session={self.alice}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.headers["Content-Type"], "image/jpeg")
        req = urllib.request.Request(f"{self.base}/api/stockpaper/img?id=../../etc/passwd",
                                     headers={"Cookie": f"faam_session={self.alice}"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(cm.exception.code, 404)
        cm.exception.close()



class FakeSite:
    """Stands in for the Netlify /api/paper function: same contract, in memory."""

    def __init__(self, token):
        import http.server
        site = self
        self.token, self.posts, self.images, self.requests = token, [], {}, []
        self.delay = 0.0                    # seconds per sync, to mimic a real network

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def _reply(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                site.requests.append((self.command, self.path))
                if self.headers.get("Authorization") != f"Bearer {site.token}":
                    return self._reply(401, {"error": "not allowed"})
                if self.command == "POST" and self.path == "/api/paper/sync":
                    if site.delay:
                        __import__("time").sleep(site.delay)
                    site.posts = json.loads(body)["posts"]
                    wanted = {p["image"] for p in site.posts if p.get("image")}
                    site.images = {k: v for k, v in site.images.items() if k in wanted}
                    return self._reply(200, {"ok": True, "count": len(site.posts),
                                             "missingImages": sorted(wanted - set(site.images))})
                if self.command == "PUT" and self.path.startswith("/api/paper/img/"):
                    site.images[self.path.rsplit("/", 1)[1]] = body
                    return self._reply(200, {"ok": True})
                return self._reply(404, {"error": "not found"})

            do_POST = do_PUT = _handle

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class TestWebsiteMirror(StockPaperCase):
    """Publishing in the app makes the story live on the public site."""

    TOKEN = "s" * 40

    def setUp(self):
        super().setUp()
        self.site = FakeSite(self.TOKEN)
        self.m.PAPER_SITE_TOKEN_FILE.write_text(self.TOKEN)
        self.m.PAPER_SITE_URL_FILE.write_text(self.site.url)
        self.m._PS.update(running=False, dirty=False, last=None)

    def tearDown(self):
        self.site.close()
        super().tearDown()

    def wait_idle(self):
        import time as _t
        for _ in range(100):
            if not self.m._PS["running"]:
                return
            _t.sleep(0.05)
        self.fail("background sync never finished")

    def test_publishing_pushes_the_story(self):
        self.publish(title="Fed holds rates")
        self.wait_idle()
        self.assertEqual([p["title"] for p in self.site.posts], ["Fed holds rates"])
        self.assertTrue(self.m._PS["last"]["ok"])

    def test_only_public_fields_leave_the_mac(self):
        self.publish()
        self.wait_idle()
        self.assertNotIn("author", self.site.posts[0])
        self.assertNotIn("commentList", self.site.posts[0])

    def test_pictures_are_uploaded_once(self):
        self.m.SP_IMG_DIR.mkdir(parents=True, exist_ok=True)
        self.m._sp_image_path("0123456789abcdef").write_bytes(b"\xff\xd8\xffjpeg")
        self.m.stockpaper_publish("dev", "With picture", "A story body", "Market", "",
                                  False, False, "0123456789abcdef")
        self.wait_idle()
        self.assertEqual(self.site.images["0123456789abcdef"], b"\xff\xd8\xffjpeg")
        puts = sum(1 for m, _ in self.site.requests if m == "PUT")
        self.m.paper_site_sync_now()
        self.assertEqual(sum(1 for m, _ in self.site.requests if m == "PUT"), puts,
                         "a picture the site already has must not be re-sent")

    def test_deleting_removes_it_from_the_site(self):
        pid = self.publish()["post"]["id"]
        self.wait_idle()
        self.m.stockpaper_delete("dev", pid)
        self.wait_idle()
        self.assertEqual(self.site.posts, [])

    def test_comment_counts_follow(self):
        pid = self.publish()["post"]["id"]
        self.wait_idle()
        self.m.stockpaper_comment("alice", pid, "Nice")
        self.wait_idle()
        self.assertEqual(self.site.posts[0]["comments"], 1)

    def test_bursts_are_coalesced(self):
        # Coalescing merges pushes that overlap. Locally a push takes ~1ms, so
        # give the fake site real-network latency or nothing ever overlaps.
        self.site.delay = 0.3
        for i in range(6):
            self.publish(title=f"Story number {i}")
        self.wait_idle()
        syncs = sum(1 for m, p in self.site.requests if p == "/api/paper/sync")
        self.assertLess(syncs, 6, "six quick publishes should not mean six pushes")
        self.assertEqual(len(self.site.posts), 6, "but the site must end up with all six")

    def test_wrong_token_is_reported_not_raised(self):
        self.m.PAPER_SITE_TOKEN_FILE.write_text("w" * 40)
        res = self.m.paper_site_sync_now()
        self.assertFalse(res["ok"])
        self.assertIn("not allowed", res["error"])

    def test_site_down_is_reported_not_raised(self):
        self.m.PAPER_SITE_URL_FILE.write_text("http://127.0.0.1:1")
        res = self.m.paper_site_sync_now()
        self.assertFalse(res["ok"])
        self.assertIn("couldn't reach", res["error"])

    def test_no_token_means_no_network(self):
        self.m.PAPER_SITE_TOKEN_FILE.unlink()
        self.publish()
        self.assertFalse(self.m._PS["running"])
        self.assertEqual(self.site.requests, [])

    def test_status_is_dev_only(self):
        port = 8832
        httpd = self.m.ThreadingHTTPServer(("127.0.0.1", port), self.m.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            def listing(user):
                req = urllib.request.Request(f"http://127.0.0.1:{port}/api/stockpaper",
                                             headers={"Cookie": f"faam_session={self.m.make_session(user)}"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    return json.loads(r.read())
            self.assertIn("site", listing("dev"))
            self.assertNotIn("site", listing("alice"))
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
