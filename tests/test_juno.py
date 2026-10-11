"""Tests for Llama X JUNO's FAAM side: signals, bands, prompts, the training-data
splits, the license naming, and forecasts against a stand-in JUNO server.

Never loads the real model or touches the network. Run from the repo root:

    python3 -m unittest tests.test_juno -v
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import random
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8836


def load(home: str, name: str, path: Path):
    os.environ["HOME"] = home
    os.environ["FAAM_DATA_DIR"] = str(Path(home) / ".faam")
    os.environ["OPENAI_API_KEY"] = ""
    os.environ["FAAM_DESKTOP_NOTIFY"] = "0"
    os.environ["JUNO_HOME"] = str(Path(home) / ".faam" / "juno")
    os.environ["FAAM_JUNO_URL"] = f"http://127.0.0.1:{PORT}"
    spec = importlib.util.spec_from_file_location(f"{name}_{abs(hash(home))}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def series(n=400, start=100.0, drift=0.001, vol=0.01, seed=1):
    rng = random.Random(seed)
    out, p = [], start
    for _ in range(n):
        p *= math.exp(drift + rng.gauss(0, vol))
        out.append(round(p, 4))
    return out


class FakeJuno(BaseHTTPRequestHandler):
    probs = {"A": 0.05, "B": 0.1, "C": 0.2, "D": 0.4, "E": 0.25}
    prompts: list = []

    def log_message(self, *a):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send({"ok": True, "name": "Llama X JUNO", "tuned": True,
                    "bandMeanZ": {"A": -1.9, "B": -0.6, "C": 0.0, "D": 0.6, "E": 1.6}})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeJuno.prompts.append(body["prompt"])
        self._send({"probs": FakeJuno.probs})


class Case(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="faam-juno-")
        (Path(self.home) / ".faam").mkdir(parents=True, exist_ok=True)
        self.m = load(self.home, "faamj", ROOT / "app.py")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)


class TestSignals(Case):

    def test_needs_enough_history(self):
        self.assertIsNone(self.m.juno_features(series(100)))
        self.assertIsNotNone(self.m.juno_features(series(140)))

    def test_rising_stock_reads_as_rising(self):
        f = self.m.juno_features(series(400, drift=0.004, vol=0.002))
        self.assertGreater(f["r21"], 0)
        self.assertGreater(f["s50"], 0)
        self.assertGreater(f["rsi"], 50)

    def test_market_and_volume_are_optional(self):
        f = self.m.juno_features(series(300), volumes=[1000] * 300, market=series(80, seed=9))
        self.assertIn("volRatio", f)
        self.assertIn("m21", f)
        self.assertNotIn("m21", self.m.juno_features(series(300)))

    def test_bands(self):
        sigma = 0.01
        edges = self.m.juno_band_edges(sigma, 21)
        self.assertEqual(edges, sorted(edges))
        self.assertAlmostEqual(edges[0], -edges[3], places=1)
        self.assertEqual(self.m.juno_band(0.0, sigma, 21), "C")
        self.assertEqual(self.m.juno_band(edges[3] + 1, sigma, 21), "E")
        self.assertEqual(self.m.juno_band(edges[0] - 1, sigma, 21), "A")

    def test_prompt_never_names_the_stock_or_date(self):
        p = self.m.juno_prompt(self.m.juno_features(series(300), market=series(80)), 21)
        self.assertIn("next 21 trading days", p)
        self.assertIn("Bands: A below", p)
        self.assertNotRegex(p, r"\b(19|20)\d\d-\d\d-\d\d\b")

    def test_expected_return(self):
        self.assertGreater(self.m.juno_expected_return({"E": 1.0}, 0.01, 21), 0)
        self.assertLess(self.m.juno_expected_return({"A": 1.0}, 0.01, 21), 0)
        self.assertAlmostEqual(self.m.juno_expected_return({"C": 1.0}, 0.01, 21), 0, places=6)


class TestLicenseNaming(Case):

    def test_name_starts_with_llama(self):
        # Llama license 1.b.i: a fine-tuned model made available must start with "Llama".
        self.assertTrue(self.m.JUNO_NAME.startswith("Llama"))
        spec = next(x for x in self.m.FORECAST_MODELS if x["id"] == "juno")
        self.assertTrue(spec["name"].startswith("Llama"))
        self.assertIn("Built with Llama", spec["kind"])
        js = (ROOT / "static" / "app.js").read_text()
        self.assertIn('name: "Llama X JUNO"', js)

    def test_license_ships_with_the_juno_code(self):
        text = (ROOT / "juno" / "LICENSE-LLAMA.txt").read_text()
        self.assertIn("LLAMA 3.1 COMMUNITY LICENSE AGREEMENT", text)


class TestHeavyTesting(Case):
    """JUNO hasn't beaten the baselines yet, so FAAM says it's in heavy testing
    everywhere it shows up."""

    def test_status_and_model_list_say_testing(self):
        self.assertTrue(self.m.juno_status()["testing"])
        spec = next(x for x in self.m.forecast_models_public() if x["id"] == "juno")
        self.assertTrue(spec["testing"])
        self.assertIn("heavy testing", self.m.SB_MODELS["juno"]["desc"])

    def test_changelog_and_roadmap_say_heavy_testing(self):
        top = self.m.CHANGELOG[0]
        self.assertIn("JUNO", top["title"])
        self.assertTrue(any("Llama X JUNO" in i["text"] and "heavy testing" in i["text"]
                            and "Built with Llama" in i["text"] for i in top["items"]))
        juno = next(r for r in self.m.ROADMAP if r["title"].startswith("Llama X JUNO"))
        self.assertIn("heavy testing", juno["text"])

    def test_agent_doesnt_claim_to_switch_to_juno(self):
        cmd = self.m.AGENT_BY_NAME["choose_forecast_model"]
        self.assertIn("heavy testing", self.m._agent_say(cmd, {"model": "juno"}))
        self.assertEqual(self.m._agent_say(cmd, {"model": "apollo"}), "Switching to apollo.")
        self.m.juno_installed = lambda: True
        self.assertEqual(self.m._agent_say(cmd, {"model": "juno"}), "Switching to juno.")

    def test_app_labels_it(self):
        js = (ROOT / "static" / "app.js").read_text()
        self.assertIn("In heavy testing", js)
        self.assertIn("testing: true", js)


class TestTrainingData(Case):

    def setUp(self):
        super().setUp()
        self.b = load(self.home, "jbd", ROOT / "juno" / "build_data.py")

    def bars(self, start="2015-01-01", n=2600):
        from datetime import date, timedelta
        d0 = date.fromisoformat(start)
        closes = series(n, seed=3)
        return [{"d": (d0 + timedelta(days=i)).isoformat(), "c": c, "v": 1000} for i, c in enumerate(closes)]

    def test_label_windows_never_cross_a_split(self):
        bars = self.bars()
        idx = {b["d"]: i for i, b in enumerate(bars)}
        mk = {b["d"]: [x["c"] for x in bars[max(0, i - 69): i + 1]] for i, b in enumerate(bars)}
        rows = self.b.forecast_examples(self.m, "TEST", bars, mk, random.Random(1))
        self.assertTrue(rows)
        for r in rows:
            end = bars[idx[r["date"]] + r["days"]]["d"]
            if r["split"] == "train":
                self.assertLess(end, self.b.TRAIN_END)
            elif r["split"] == "valid":
                self.assertGreaterEqual(r["date"], self.b.TRAIN_END)
                self.assertLess(end, self.b.VALID_END)
            else:
                self.assertGreaterEqual(r["date"], self.b.TEST_START)

    def test_examples_hide_ticker_and_date(self):
        bars = self.bars()
        mk = {b["d"]: [x["c"] for x in bars[max(0, i - 69): i + 1]] for i, b in enumerate(bars)}
        for r in self.b.forecast_examples(self.m, "NVDA", bars, mk, random.Random(2))[:50]:
            self.assertNotIn("NVDA", r["prompt"])
            self.assertNotIn(r["date"], r["prompt"])
            self.assertIn(r["band"], "ABCDE")

    def test_explain_examples_come_from_faam(self):
        ex = self.b.explain_examples(self.m)
        self.assertGreater(len(ex), 100)
        self.assertTrue(all(e["messages"][0]["content"].startswith("You are Llama X JUNO") for e in ex))


class TestForecast(Case):

    def setUp(self):
        super().setUp()
        hist = [{"t": 1_600_000_000 + i * 86400, "c": c, "v": 1000} for i, c in enumerate(series(500, drift=0.001))]
        spy = [{"t": 0, "c": c} for c in series(130, seed=5)]
        self.m.yahoo_quote = lambda sym, range_="1mo", interval="1d": (
            {"symbol": "SPY", "history": spy} if sym == "SPY" else {"symbol": sym, "name": "Test Co", "history": hist})
        self.httpd = None

    def tearDown(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        super().tearDown()

    def serve(self):
        FakeJuno.prompts = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", PORT), FakeJuno)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def test_coming_soon_until_installed(self):
        juno = next(x for x in self.m.forecast_models_public() if x["id"] == "juno")
        self.assertTrue(juno["comingSoon"])
        self.m.juno_installed = lambda: True
        juno = next(x for x in self.m.forecast_models_public() if x["id"] == "juno")
        self.assertTrue(juno["live"])
        self.assertFalse(juno["comingSoon"])

    def test_not_set_up(self):
        r = self.m.compute_forecast("TEST", 30, "juno")
        self.assertIn("in heavy testing", r["error"])

    def test_a_juno_forecast(self):
        self.serve()
        r = self.m.compute_forecast("TEST", 30, "juno")
        self.assertNotIn("error", r)
        j = r["juno"]
        self.assertEqual((j["name"], j["builtWith"], j["days"]), ("Llama X JUNO", "Built with Llama", 21))
        self.assertAlmostEqual(sum(b["p"] for b in j["bands"]), 1.0, places=3)
        self.assertAlmostEqual(j["probUp"], 0.65, places=3)
        self.assertGreater(r["stats"]["expReturnPct"], 0, "JUNO leaned up, so the cone should too")
        self.assertEqual(len(r["forecast"]), 31)
        self.assertEqual(r["model"]["name"], "Llama X JUNO")
        self.assertNotIn("TEST", FakeJuno.prompts[0], "the symbol never reaches JUNO")

    def test_horizon_maps_to_a_trained_one(self):
        self.serve()
        self.assertEqual(self.m.compute_forecast("TEST", 7, "juno")["juno"]["days"], 5)
        self.assertEqual(self.m.compute_forecast("TEST", 90, "juno")["juno"]["days"], 63)

    def test_garbage_from_juno_is_an_error_not_a_crash(self):
        self.serve()
        old = FakeJuno.probs
        try:
            FakeJuno.probs = {"A": "x"}
            self.assertIn("didn't answer", self.m.compute_forecast("TEST", 30, "juno")["error"])
            FakeJuno.probs = {}
            self.assertIn("error", self.m.compute_forecast("TEST", 30, "juno"))
        finally:
            FakeJuno.probs = old

    def test_other_models_unchanged(self):
        r = self.m.compute_forecast("TEST", 30, "apollo")
        self.assertEqual(r["model"]["id"], "apollo")
        self.assertNotIn("juno", r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
