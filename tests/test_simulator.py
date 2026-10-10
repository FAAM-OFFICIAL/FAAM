"""Tests for the dev-only stock simulator (historical replay).

Prices come from a synthetic series patched over yahoo_quote, so these tests
never touch the network. Run from the repo root:

    python3 -m unittest tests.test_simulator -v
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
    spec = importlib.util.spec_from_file_location(f"faamsim_{abs(hash(home))}", APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fake_history(n=600, start=50.0, step=0.5):
    """A steadily rising stock: day i closes at start + i*step."""
    out = []
    for i in range(n):
        c = start + i * step
        out.append({"t": 1_600_000_000 + i * 86400, "o": c - 0.1, "h": c + 0.4,
                    "l": c - 0.4, "c": c, "v": 1000})
    return out


class SimCase(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="faam-sim-")
        (Path(self.home) / ".faam").mkdir(parents=True, exist_ok=True)
        self.m = load_app(self.home)
        self.m.seed_users()
        users = self.m.load_users()
        users["alice"] = {"pw": self.m.hash_password("x"), "tier": 0, "plan": "",
                          "admin": False, "email": "", "created": 0}
        self.m.save_users(users)
        self.hist = fake_history()
        self.m.yahoo_quote = lambda sym, range_="5y", interval="1d": {
            "name": f"{sym} Inc.", "history": self.hist}
        self.m._SIM.clear()

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def start(self, **kw):
        args = dict(symbol="", length="short", blind=True)
        args.update(kw)
        return self.m.sim_start("dev", **args)


class TestAccess(SimCase):

    def test_dev_can_start(self):
        self.assertTrue(self.start()["ok"])

    def test_others_get_not_found(self):
        r = self.m.sim_start("alice")
        self.assertEqual(r["status"], 404)
        self.assertEqual(r["error"], "Not found.")

    def test_others_cannot_step_a_dev_session(self):
        sid = self.start()["id"]
        self.assertEqual(self.m.sim_step("alice", sid, "hold", 0, 1)["status"], 404)

    def test_unknown_session(self):
        self.assertEqual(self.m.sim_step("dev", "nope", "hold", 0, 1)["status"], 404)


class TestNoPeeking(SimCase):
    """The whole point of a replay: the page must never hold future prices."""

    def test_start_sends_only_the_warmup_and_day_zero(self):
        r = self.start()
        self.assertEqual(len(r["candles"]), self.m.SIM_WARMUP + 1)
        self.assertEqual(r["candles"][-1]["day"], 0)

    def test_each_step_sends_only_the_days_that_passed(self):
        sid = self.start()["id"]
        r = self.m.sim_step("dev", sid, "hold", 0, 5)
        self.assertEqual([c["day"] for c in r["candles"]], [1, 2, 3, 4, 5])

    def test_trading_alone_does_not_advance(self):
        sid = self.start()["id"]
        r = self.m.sim_step("dev", sid, "buy", 1, 0)
        self.assertEqual(r["day"], 0)
        self.assertEqual(r["candles"], [])

    def test_mystery_mode_hides_identity(self):
        r = self.start(blind=True)
        self.assertIsNone(r["symbol"])
        self.assertTrue(all("t" not in c for c in r["candles"]), "dates leaked")
        self.assertEqual(r["price"], 100.0, "mystery prices should be rebased to $100")
        self.assertNotIn("reveal", r)

    def test_named_mode_shows_identity(self):
        r = self.start(blind=False, symbol="NVDA")
        self.assertEqual(r["symbol"], "NVDA")
        self.assertTrue(all("t" in c for c in r["candles"]))


class TestTrading(SimCase):

    def setUp(self):
        super().setUp()
        self.sid = self.start()["id"]

    def step(self, *a):
        return self.m.sim_step("dev", self.sid, *a)

    def test_buy_moves_cash_into_shares(self):
        r = self.step("buy", 10, 0)
        self.assertEqual(r["shares"], 10)
        self.assertAlmostEqual(r["cash"], self.m.SIM_CASH - 10 * 100.0, places=2)

    def test_cannot_overspend(self):
        self.assertEqual(self.step("buy", 101, 0)["status"], 400)   # 101 * $100 > $10,000

    def test_cannot_sell_what_you_dont_hold(self):
        self.assertEqual(self.step("sell", 1, 0)["status"], 400)

    def test_zero_or_bad_quantity_refused(self):
        self.assertEqual(self.step("buy", 0, 0)["status"], 400)
        self.assertEqual(self.step("buy", "lots", 0)["status"], 400)

    def test_trades_are_logged(self):
        self.step("buy", 5, 0)
        r = self.step("sell", 2, 0)
        self.assertEqual([t["side"] for t in r["trades"]], ["buy", "sell"])

    def test_equity_tracks_price(self):
        self.step("buy", 50, 0)
        r = self.step("hold", 0, 10)
        expected = r["cash"] + r["shares"] * r["price"]
        self.assertAlmostEqual(r["equity"], expected, places=1)

    def test_all_in_matches_buy_and_hold(self):
        self.step("buy", 100, 0)                       # every dollar at day 0
        r = self.step("hold", 0, "end")
        self.assertAlmostEqual(r["returnPct"], r["holdPct"], places=1)

    def test_advance_is_capped(self):
        r = self.step("hold", 0, 999)
        self.assertLessEqual(r["day"], 30)


class TestFinish(SimCase):

    def test_run_ends_with_a_reveal(self):
        sid = self.start()["id"]
        r = self.m.sim_step("dev", sid, "hold", 0, "end")
        self.assertTrue(r["done"])
        self.assertEqual(r["day"], self.m.SIM_LENGTHS["short"])
        self.assertIn(r["reveal"]["symbol"], self.m.SIM_POOL)
        self.assertGreater(r["reveal"]["end"], r["reveal"]["start"])

    def test_cannot_step_after_the_end(self):
        sid = self.start()["id"]
        self.m.sim_step("dev", sid, "hold", 0, "end")
        self.assertEqual(self.m.sim_step("dev", sid, "hold", 0, 1)["status"], 400)

    def test_lengths(self):
        for name, n in self.m.SIM_LENGTHS.items():
            with self.subTest(length=name):
                self.assertEqual(self.start(length=name)["days"], n)

    def test_short_history_is_refused(self):
        self.hist = fake_history(n=50)
        self.assertEqual(self.start(symbol="TINY")["status"], 400)

    def test_bad_ticker_is_refused(self):
        self.assertEqual(self.start(symbol="../etc")["status"], 400)

    def test_drawdown_reflects_a_fall(self):
        # Rise then crash: holding through it must show a real drawdown.
        up = fake_history(n=300, start=50, step=0.5)
        down = [{**p, "c": 200 - i * 1.0, "t": p["t"] + 300 * 86400}
                for i, p in enumerate(fake_history(n=300, start=50, step=0))]
        self.hist = up + down
        sid = None
        for _ in range(20):           # find a window that spans the crash
            r = self.start(length="long")
            st = self.m._SIM[r["id"]]
            closes = [p["c"] for p in st["window"][self.m.SIM_WARMUP:]]
            if max(closes) - closes[-1] > 20:
                sid = r["id"]
                break
        self.assertIsNotNone(sid, "no window spanned the crash")
        self.m.sim_step("dev", sid, "buy", 100, 0)
        r = self.m.sim_step("dev", sid, "hold", 0, "end")
        self.assertLess(r["reveal"]["maxDrawdownPct"], -5)


class TestEndpoints(SimCase):

    def setUp(self):
        super().setUp()
        self.port = 8831
        self.base = f"http://127.0.0.1:{self.port}"
        self.httpd = self.m.ThreadingHTTPServer(("127.0.0.1", self.port), self.m.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.dev = self.m.make_session("dev")
        self.alice = self.m.make_session("alice")

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def call(self, path, body, session=None):
        h = {"content-type": "application/json", "Origin": self.base}
        if session:
            h["Cookie"] = f"faam_session={session}"
        req = urllib.request.Request(self.base + path, headers=h)
        try:
            with urllib.request.urlopen(req, json.dumps(body).encode(), timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_hidden_from_everyone_but_dev(self):
        self.assertEqual(self.call("/api/sim/start", {})[0], 404)
        self.assertEqual(self.call("/api/sim/start", {}, self.alice)[0], 404)

    def test_dev_flow_over_http(self):
        code, r = self.call("/api/sim/start", {"length": "short"}, self.dev)
        self.assertEqual(code, 200)
        code, r = self.call("/api/sim/step",
                            {"id": r["id"], "action": "buy", "shares": 3, "advance": 1}, self.dev)
        self.assertEqual(code, 200)
        self.assertEqual((r["shares"], r["day"]), (3, 1))
        code, r = self.call("/api/sim/step", {"id": r["id"], "advance": "end"}, self.dev)
        self.assertTrue(r["done"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
