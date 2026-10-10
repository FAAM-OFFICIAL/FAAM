"""Tests for the Stocks Browser (AI web research) and price alerts/notifications.

The AI, quotes and headlines are all faked, so these never touch the network
or cost anything. Run from the repo root:

    python3 -m unittest tests.test_browser_alerts -v
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
    os.environ["FAAM_DESKTOP_NOTIFY"] = "0"
    spec = importlib.util.spec_from_file_location(f"faamsb_{abs(hash(home))}", APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ai_result(text, cites=(), sources=(), searches=1):
    """A Responses API result shaped like the real one (see the live sample)."""
    anns = []
    for title, url, marker in cites:
        start = text.index(marker)
        anns.append({"type": "url_citation", "start_index": start,
                     "end_index": start + len(marker), "url": url, "title": title})
    out = [{"type": "web_search_call", "status": "completed",
            "action": {"type": "search", "query": "q",
                       "sources": [{"type": "url", "url": u} for u in sources]}}
           for _ in range(searches)]
    out.append({"type": "message", "content": [
        {"type": "output_text", "text": text, "annotations": anns}]})
    return {"output": out, "usage": {"input_tokens": 8000, "output_tokens": 500}}


YF = "https://finance.yahoo.com/news/nvda-rally-1.html?utm_source=openai"
WSJ = "https://www.wsj.com/tech/nvidia-chips-2?utm_source=openai&mod=x"
SAMPLE = (
    "Nvidia rose after an analyst upgrade. ([finance.yahoo.com](" + YF + "))\n\n"
    "Chip demand stayed strong. ([wsj.com](" + WSJ + "))\n\n"
    "Investors also watched rates. ([finance.yahoo.com](" + YF + "))\n"
    "TICKERS: NVDA, $AMD"
)


def sample(**kw):
    return ai_result(SAMPLE, cites=[
        ("Nvidia rallies", YF, "([finance.yahoo.com](" + YF + "))"),
        ("Chips stay hot", WSJ, "([wsj.com](" + WSJ + "))"),
    ], sources=[YF, WSJ, "https://finance.yahoo.com/quote/NVDA/"], **kw)


class Case(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="faam-sb-")
        (Path(self.home) / ".faam").mkdir(parents=True, exist_ok=True)
        self.m = load_app(self.home)
        self.m.seed_users()
        users = self.m.load_users()
        for name in ("alice", "bob"):
            users[name] = {"pw": self.m.hash_password("x"), "tier": 0, "plan": "",
                           "admin": False, "email": "", "created": 0}
        self.m.save_users(users)
        self.prices = {"NVDA": (100.0, 1.0), "AAPL": (200.0, -0.5), "AMD": (150.0, 0.0)}

        def quote(sym, range_="1mo", interval="1d"):
            if sym not in self.prices:
                return {"error": "unknown"}
            p, pct = self.prices[sym]
            return {"symbol": sym, "name": {"NVDA": "NVIDIA Corporation"}.get(sym, f"{sym} Inc."), "price": p, "change": p * pct / 100,
                    "pct": pct, "history": [{"c": p - 2}, {"c": p - 1}, {"c": p}]}
        self.m.yahoo_quote = quote
        self.m.fetch_news = lambda sym, limit=8: [
            {"title": f"{sym} headline", "publisher": "Wire", "link": "https://example.com/a", "time": 1}]
        self.costs = []
        self.m.record_cost = self.costs.append
        self.desktop = []
        self.m.desktop_notify = lambda t, b: self.desktop.append((t, b))
        self.m._SB_CACHE.clear()
        self.m._SB_RATE.clear()

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)


class TestParsing(Case):

    def test_citations_become_numbered_markers(self):
        r = self.m.sb_parse(sample())
        self.assertEqual(r["paragraphs"][0], "Nvidia rose after an analyst upgrade.[[1]]")
        self.assertEqual(r["paragraphs"][1], "Chip demand stayed strong.[[2]]")
        self.assertEqual(r["paragraphs"][2], "Investors also watched rates.[[1]]", "same page, same number")
        self.assertEqual([s["n"] for s in r["sources"]], [1, 2])

    def test_tracking_parameters_removed(self):
        r = self.m.sb_parse(sample())
        self.assertEqual(r["sources"][0]["url"], "https://finance.yahoo.com/news/nvda-rally-1.html")
        self.assertEqual(r["sources"][1]["url"], "https://www.wsj.com/tech/nvidia-chips-2?mod=x")
        self.assertEqual(r["sources"][1]["domain"], "wsj.com")

    def test_ticker_line_is_read_and_removed(self):
        r = self.m.sb_parse(sample())
        self.assertEqual(r["tickers"], ["NVDA", "AMD"])
        self.assertFalse(any("TICKERS" in p for p in r["paragraphs"]))

    def test_ticker_line_at_the_end_of_a_paragraph(self):
        r = self.m.sb_parse(ai_result("Apple beat estimates. It may be out of date. TICKERS: AAPL"))
        self.assertEqual(r["tickers"], ["AAPL"])
        self.assertEqual(r["paragraphs"], ["Apple beat estimates. It may be out of date."])

    def test_pages_looked_at_but_not_cited(self):
        r = self.m.sb_parse(sample())
        self.assertEqual([m["url"] for m in r["more"]], ["https://finance.yahoo.com/quote/NVDA/"])

    def test_unsafe_links_never_become_sources(self):
        bad = "javascript:alert(1)"
        text = "Hi. ([x](" + bad + "))"
        r = self.m.sb_parse(ai_result(text, cites=[("x", bad, "([x](" + bad + "))")],
                                      sources=["data:text/html,hi", "file:///etc/passwd"]))
        self.assertEqual(r["sources"], [])
        self.assertEqual(r["more"], [])
        self.assertNotIn("javascript", " ".join(r["paragraphs"]))

    def test_markdown_links_without_annotations_still_cite(self):
        r = self.m.sb_parse(ai_result("Up 3% ([reuters.com](https://www.reuters.com/x)) on **Monday**."))
        self.assertEqual(r["paragraphs"], ["Up 3%[[1]] on Monday."])
        self.assertEqual(r["sources"][0]["domain"], "reuters.com")

    def test_paragraph_labels_are_removed(self):
        r = self.m.sb_parse(ai_result(
            "Direct answer: chips rallied.\n\nWhat to watch next (may change): earnings.\n\n"
            "Summary statistics look fine."))
        self.assertEqual(r["paragraphs"], ["Chips rallied.", "Earnings.", "Summary statistics look fine."])

    def test_headlines_must_be_about_the_company(self):
        self.m.fetch_news = lambda sym, limit=8: [
            {"title": "Oil tops $100", "link": "https://x/1"},
            {"title": "Nvidia beats estimates", "link": "https://x/2"},
            {"title": "Why NVDA fell", "link": "https://x/3"},
            {"title": "Moderna surges", "link": "https://x/4"}]
        card = self.m.sb_quote_card(["NVDA"])
        self.assertEqual([n["title"] for n in card["news"]], ["Nvidia beats estimates", "Why NVDA fell"])

    def test_empty_links_are_removed(self):
        r = self.m.sb_parse(ai_result("Shares fell. ([]()) Also [](x) here ()."))
        self.assertEqual(r["paragraphs"], ["Shares fell. Also here."])

    def test_misaligned_annotation_is_ignored(self):
        res = ai_result("Short text.")
        res["output"][-1]["content"][0]["annotations"] = [
            {"type": "url_citation", "start_index": 5, "end_index": 999, "url": YF, "title": "t"}]
        self.assertEqual(self.m.sb_parse(res)["paragraphs"], ["Short text."])


class TestSearch(Case):

    def setUp(self):
        super().setUp()
        self.m.OPENAI_API_KEY = "test-key"
        self.calls = []

        def fake(instructions, prompt, domains):
            self.calls.append({"instructions": instructions, "prompt": prompt, "domains": domains})
            return sample()
        self.m._sb_responses = fake

    def test_a_search(self):
        r = self.m.stocks_browser_search("alice", "why is nvda up", "web")
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["paragraphs"]), 3)
        self.assertEqual(r["quote"]["symbol"], "NVDA")
        self.assertEqual(r["quote"]["news"][0]["title"], "NVDA headline")
        self.assertEqual(self.calls[0]["domains"], [], "all the web = no filter")
        self.assertIn("never tell the reader to buy", self.calls[0]["instructions"].lower())

    def test_sources_filter_the_search(self):
        self.m.stocks_browser_search("alice", "nvidia", "yahoo")
        self.m.stocks_browser_search("alice", "nvidia", "wsj")
        self.assertEqual(self.calls[0]["domains"], ["finance.yahoo.com"])
        self.assertEqual(self.calls[1]["domains"], ["wsj.com"])

    def test_unknown_source_falls_back_to_the_web(self):
        r = self.m.stocks_browser_search("alice", "nvidia", "evil")
        self.assertEqual((r["source"], self.calls[0]["domains"]), ("web", []))

    def test_cost_includes_the_search_fee(self):
        self.m.stocks_browser_search("alice", "nvidia")
        expected = 8000 / 1e6 * 0.25 + 500 / 1e6 * 2.00 + 0.01
        self.assertAlmostEqual(self.costs[0], expected, places=6)

    def test_repeat_searches_are_cached_and_free(self):
        self.m.stocks_browser_search("alice", "Nvidia news")
        r = self.m.stocks_browser_search("bob", "  nvidia   NEWS ")
        self.assertTrue(r["cached"])
        self.assertEqual((len(self.calls), len(self.costs)), (1, 1))

    def test_different_sources_are_not_shared_in_cache(self):
        self.m.stocks_browser_search("alice", "nvidia", "web")
        self.m.stocks_browser_search("alice", "nvidia", "wsj")
        self.assertEqual(len(self.calls), 2)

    def test_summarize_a_page(self):
        r = self.m.stocks_browser_search("alice", "Nvidia rallies", url=YF)
        self.assertTrue(r["ok"])
        self.assertEqual(self.calls[0]["domains"], ["finance.yahoo.com"])
        self.assertIn("summary of one specific page", self.calls[0]["instructions"])
        self.assertIn("https://finance.yahoo.com/news/nvda-rally-1.html", self.calls[0]["prompt"])

    def test_bad_input(self):
        self.assertEqual(self.m.stocks_browser_search("alice", " ")["status"], 400)
        self.assertEqual(self.m.stocks_browser_search("alice", "x", url="javascript:alert(1)")["status"], 400)
        self.assertEqual(self.calls, [])

    def test_long_queries_are_trimmed(self):
        self.m.stocks_browser_search("alice", "a" * 5000)
        self.assertLess(len(self.calls[0]["prompt"]), 400)

    def test_no_key(self):
        self.m.OPENAI_API_KEY = ""
        self.assertEqual(self.m.stocks_browser_search("alice", "nvidia")["status"], 503)

    def test_ai_failure(self):
        self.m._sb_responses = lambda *a: {"error": "AI service error 500"}
        self.assertEqual(self.m.stocks_browser_search("alice", "nvidia")["status"], 502)
        self.m._sb_responses = lambda *a: ai_result("")
        self.assertEqual(self.m.stocks_browser_search("alice", "nvidia 2")["status"], 502)

    def test_rate_limit(self):
        for i in range(self.m.SB_HOURLY):
            self.assertTrue(self.m.stocks_browser_search("alice", f"q{i}")["ok"])
        self.assertEqual(self.m.stocks_browser_search("alice", "one more")["status"], 429)
        self.assertTrue(self.m.stocks_browser_search("bob", "one more")["ok"], "per person")

    def test_no_quote_when_nothing_resolves(self):
        self.m._sb_responses = lambda *a: ai_result("Rates explained.\nTICKERS: NONE")
        self.assertIsNone(self.m.stocks_browser_search("alice", "what are rates")["quote"])


class TestAlerts(Case):

    def make(self, user="alice", sym="NVDA", kind="above", value=110):
        r = self.m.alerts_create(user, sym, kind, value)
        self.assertTrue(r.get("ok"), r)
        return r

    def test_validation(self):
        for args, why in [(("../x", "above", 1), "symbol"), (("NVDA", "sideways", 1), "kind"),
                          (("NVDA", "above", "lots"), "number"), (("NVDA", "above", 0), "price"),
                          (("NVDA", "above", float("nan")), "nan"), (("NVDA", "up", 500), "pct"),
                          (("ZZZZ", "above", 5), "unknown symbol")]:
            with self.subTest(why=why):
                self.assertEqual(self.m.alerts_create("alice", *args)["status"], 400)

    def test_create_reports_the_price_and_whether_it_is_already_met(self):
        r = self.make(value=90)
        self.assertEqual(r["price"], 100.0)
        self.assertTrue(r["alreadyMet"])
        self.assertEqual(r["alert"]["label"], "NVDA rises to $90.00")
        self.assertFalse(self.make(value=150)["alreadyMet"])

    def test_fires_once_when_crossed(self):
        self.make(value=110)
        self.assertEqual(self.m.alerts_check_once(), 0)
        self.prices["NVDA"] = (112.0, 3.0)
        self.assertEqual(self.m.alerts_check_once(), 1)
        self.assertEqual(self.m.alerts_check_once(), 0, "one-shot until re-armed")
        n = self.m.notifs_list("alice")
        self.assertEqual(n["unread"], 1)
        self.assertEqual(n["items"][0]["symbol"], "NVDA")
        self.assertIn("$112.00", n["items"][0]["body"])
        self.assertEqual(len(self.desktop), 1)
        a = self.m.alerts_list("alice")[0]
        self.assertEqual((a["active"], a["triggerPrice"]), (False, 112.0))

    def test_each_kind(self):
        self.make(sym="AAPL", kind="below", value=190)
        self.make(sym="NVDA", kind="up", value=5)
        self.make(sym="AMD", kind="down", value=4)
        self.prices.update(AAPL=(189.0, -5.5), NVDA=(106.0, 6.0), AMD=(140.0, -6.7))
        self.assertEqual(self.m.alerts_check_once(), 3)

    def test_rearm(self):
        aid = self.make(value=110)["alert"]["id"]
        self.prices["NVDA"] = (120.0, 20.0)
        self.m.alerts_check_once()
        self.m.alerts_rearm("alice", aid)
        self.assertEqual(self.m.alerts_check_once(), 1)

    def test_alerts_are_private(self):
        aid = self.make()["alert"]["id"]
        self.assertEqual(self.m.alerts_list("bob"), [])
        self.assertEqual(self.m.alerts_delete("bob", aid)["status"], 404)
        self.assertEqual(self.m.alerts_rearm("bob", aid)["status"], 404)
        self.prices["NVDA"] = (120.0, 20.0)
        self.m.alerts_check_once()
        self.assertEqual(self.m.notifs_list("bob")["items"], [])

    def test_delete(self):
        aid = self.make()["alert"]["id"]
        self.assertEqual(self.m.alerts_delete("alice", aid)["alerts"], [])

    def test_limit(self):
        self.m.ALERTS_MAX = 3
        for _ in range(3):
            self.make()
        self.assertEqual(self.m.alerts_create("alice", "NVDA", "above", 1)["status"], 400)

    def test_a_failed_quote_does_not_fire(self):
        self.make(value=1)
        self.m.yahoo_quote = lambda *a, **k: (_ for _ in ()).throw(OSError("offline"))
        self.assertEqual(self.m.alerts_check_once(), 0)


class TestNotifications(Case):

    def test_read_and_clear(self):
        a = self.m.notify("alice", "One", "x")
        self.m.notify("alice", "Two", "y")
        self.assertEqual(self.m.notifs_list("alice")["unread"], 2)
        self.assertEqual(self.m.notifs_read("alice", [a["id"]])["unread"], 1)
        self.assertEqual(self.m.notifs_read("alice")["unread"], 0)
        self.assertEqual(self.m.notifs_clear("alice")["items"], [])

    def test_newest_first_and_capped(self):
        self.m.NOTIFS_MAX = 5
        for i in range(8):
            self.m.notify("alice", f"n{i}", "")
        self.assertEqual([n["title"] for n in self.m.notifs_list("alice")["items"]],
                         ["n7", "n6", "n5", "n4", "n3"])

    def test_new_stock_paper_story_notifies_everyone_but_the_author(self):
        r = self.m.stockpaper_publish("dev", "Chips rally into earnings",
                                      "Semiconductor shares climbed this week as…", "Market", "NVDA")
        self.assertTrue(r.get("ok"), r)
        for who in ("alice", "bob"):
            item = self.m.notifs_list(who)["items"][0]
            self.assertEqual((item["kind"], item["body"], item["ref"]),
                             ("paper", "Chips rally into earnings", r["post"]["id"]))
        self.assertEqual(self.m.notifs_list("dev")["items"], [])

    def test_mac_notification_text_is_passed_as_arguments(self):
        m2 = load_app(self.home)
        m2.DESKTOP_NOTIFS = True
        seen = []

        class FakePopen:
            def __init__(self, args, **kw):
                seen.append(args)
        m2.subprocess.Popen = FakePopen
        try:
            title = 'NVDA" & (do shell script "rm -rf ~") & "'
            m2.desktop_notify(title, "body")
        finally:
            import subprocess
            m2.subprocess.Popen = subprocess.Popen
        args = seen[0]
        self.assertEqual(args[0], "/usr/bin/osascript")
        self.assertEqual(args[-2:], [title, "body"], "text travels as argv, outside the script")
        self.assertNotIn("rm -rf", args[2])


class TestHttp(Case):

    def setUp(self):
        super().setUp()
        self.port = 8833
        self.base = f"http://127.0.0.1:{self.port}"
        self.httpd = self.m.ThreadingHTTPServer(("127.0.0.1", self.port), self.m.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.alice = self.m.make_session("alice")
        self.m.OPENAI_API_KEY = "test-key"
        self.m._sb_responses = lambda *a: sample()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def call(self, path, body=None, session=None):
        h = {"Origin": self.base}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["content-type"] = "application/json"
        if session:
            h["Cookie"] = f"faam_session={session}"
        req = urllib.request.Request(self.base + path, data=data, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_signed_out_gets_nothing(self):
        for path, body in [("/api/browser/search", {"query": "x"}), ("/api/alerts/create", {}),
                           ("/api/notifications/read", {})]:
            self.assertEqual(self.call(path, body)[0], 401)
        self.assertEqual(self.call("/api/alerts")[0], 401)
        self.assertEqual(self.call("/api/notifications")[0], 401)

    def test_sources_listed(self):
        code, d = self.call("/api/browser/sources")
        self.assertIn("wsj", [s["id"] for s in d["sources"]])

    def test_search_over_http(self):
        code, d = self.call("/api/browser/search", {"query": "nvda", "source": "yahoo"}, self.alice)
        self.assertEqual(code, 200)
        self.assertEqual(d["sourceLabel"], "Yahoo Finance")
        self.assertEqual(d["quote"]["symbol"], "NVDA")

    def test_alert_to_notification_over_http(self):
        code, d = self.call("/api/alerts/create", {"symbol": "nvda", "kind": "above", "value": 105}, self.alice)
        self.assertEqual(code, 200)
        self.prices["NVDA"] = (106.0, 6.0)
        self.m.alerts_check_once()
        code, d = self.call("/api/notifications", session=self.alice)
        self.assertEqual(d["unread"], 1)
        code, d = self.call("/api/notifications/read", {}, self.alice)
        self.assertEqual(d["unread"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
