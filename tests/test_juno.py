"""Tests for JUNO's FAAM side: Llama X JUNO's signals and training data, the
license naming, JUNO Signal (the numbers model), its forecasts, and the live
scorecard.

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
import urllib.request
from datetime import date, datetime, timedelta
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


def weekdays(n, end=date(2025, 6, 30)):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def tiny_model(passed=(21,)):
    """A one-tree JUNO Signal: beat the S&P 500 over 3 months -> 60% odds, else 40%.
    A missing value goes left (40%)."""
    from importlib import import_module  # noqa: F401  (keeps the helper self-contained)
    feats = ["r5", "r21", "r63", "r126", "r252", "mom12_1", "z21", "z63", "v20", "v60", "max21", "min21",
             "s50", "s200", "rsi", "fromHigh", "fromLow", "dd60", "trendR2", "volRatio", "dollarVol",
             "ex21", "ex63", "ex126", "ex252", "beta", "corr", "idioVol", "m21", "m63", "mVol20", "m200",
             "sec21", "sec63", "rs63", "rs126"]
    lo, hi = math.log(0.4 / 0.6), math.log(0.6 / 0.4)
    tree = [[feats.index("ex63"), 0.0, 1, 2, 1], [-1, lo], [-1, hi]]
    h = lambda d: {"baseline": 0.0, "trees": [tree], "platt": [1.0, 0.0],
                   "calib": [[0.45, 0.46, -0.06, 500], [0.55, 0.5, 0.0, 500], [1.0, 0.57, 0.1, 500]],
                   "eval": {"years": "2020–2026", "tested": 1000, "ic": 0.03, "icT": 2.4, "hitRate": 0.53,
                            "auc": 0.52, "logLoss": 0.69, "baseLogLoss": 0.693, "passed": d in passed}}
    return {"name": "JUNO Signal", "version": 2, "trained": "2025-07-01", "features": feats,
            "medians": {k: 0.0 for k in feats}, "sectorEtf": {"TEST": "XLK"},
            "horizons": {str(d): h(d) for d in (5, 21, 63)}}


class Case(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="faam-juno-")
        (Path(self.home) / ".faam").mkdir(parents=True, exist_ok=True)
        self.m = load(self.home, "faamj", ROOT / "app.py")
        self.m.SIGNAL_FILE = Path(self.home) / "juno-signal.json"    # no model unless a test writes one

    def use_model(self, model):
        self.m.SIGNAL_FILE.write_text(json.dumps(model))

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
        js = (ROOT / "static" / "app.js").read_text()
        self.assertIn('name: "Llama X JUNO"', js)
        # Llama 3.1 license: "Built with Llama" shows wherever Llama's output does.
        self.assertIn("j.explainedBy ? ` Explained by ${j.explainedBy} (${j.builtWith}).`", js)

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
        self.assertTrue(any("JUNO" in i["text"] and "heavy testing" in i["text"] for i in top["items"]))
        launch = next(c for c in self.m.CHANGELOG if c["version"] == "1.11")
        self.assertTrue(any("Llama X JUNO" in i["text"] and "heavy testing" in i["text"]
                            and "Built with Llama" in i["text"] for i in launch["items"]))
        juno = next(r for r in self.m.ROADMAP if r["title"].startswith("Llama X JUNO"))
        self.assertIn("heavy testing", juno["text"])

    def test_agent_doesnt_claim_to_switch_to_juno(self):
        cmd = self.m.AGENT_BY_NAME["choose_forecast_model"]
        self.assertIn("heavy testing", self.m._agent_say(cmd, {"model": "juno"}))
        self.assertEqual(self.m._agent_say(cmd, {"model": "apollo"}), "Switching to apollo.")
        self.m.juno_installed = lambda: True          # Llama alone doesn't make JUNO forecasts pickable
        self.assertIn("heavy testing", self.m._agent_say(cmd, {"model": "juno"}))
        self.m.signal_passed = lambda model=None: [21]
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


def bars_for(closes, days):
    return [{"t": int(datetime(d.year, d.month, d.day, 14, 30).timestamp()), "c": c, "v": 1_000_000}
            for d, c in zip(days, closes)]


class TestSignal(Case):
    """JUNO Signal: the inputs, the plain-Python tree scorer, and the reasons."""

    def test_features_need_history_and_line_up(self):
        self.assertIsNone(self.m.signal_features(series(120)))
        mk = series(300, seed=9)
        f = self.m.signal_features(series(300), [1e6] * 300, mk)
        self.assertEqual(set(self.m.SIGNAL_FEATURES) | {"sigma"}, set(f))
        self.assertIsNotNone(f["beta"])
        self.assertIsNone(f["sec63"], "no sector series, no sector signals")
        self.assertIsNone(self.m.signal_features(series(300), market=mk[:-5])["beta"], "misaligned market is ignored")
        self.assertIsNotNone(self.m.signal_features(series(300), market=mk, sector=series(300, seed=4))["sec63"])

    def test_a_stock_that_tracks_the_market(self):
        mk = series(300, seed=2)
        f = self.m.signal_features([x * 2 for x in mk], [1e6] * 300, mk)
        self.assertAlmostEqual(f["beta"], 1.0, places=3)
        self.assertAlmostEqual(f["corr"], 1.0, places=3)
        self.assertAlmostEqual(f["ex63"], 0.0, places=3)

    def test_scorer_walks_the_trees(self):
        m = tiny_model()
        f = {k: None for k in m["features"]}
        self.assertAlmostEqual(self.m.signal_score(f, 21, m), 0.4, places=6, msg="missing goes left")
        f["ex63"] = 3.0
        self.assertAlmostEqual(self.m.signal_score(f, 21, m), 0.6, places=6)
        f["ex63"] = 0.0
        self.assertAlmostEqual(self.m.signal_score(f, 21, m), 0.4, places=6, msg="ties go left (x <= threshold)")
        m["horizons"]["21"]["platt"] = [0.5, 0.0]
        self.assertAlmostEqual(self.m.signal_score({"ex63": 3.0}, 21, m), 1 / (1 + (0.4 / 0.6) ** 0.5), places=6)
        self.assertIsNone(self.m.signal_score(f, 10, m), "only trained horizons")

    def test_reasons_name_what_moved_the_odds(self):
        m = tiny_model()
        f = {k: 1.0 for k in m["features"]}
        f["ex63"] = 9.4
        r = self.m.signal_reasons(f, 21, m)
        self.assertEqual(r, [{"signal": "ex63", "text": "It beat the S&P 500 by 9 points over 3 months", "push": "up"}])
        f["ex63"] = -4.0
        m["medians"]["ex63"] = 2.0
        self.assertEqual(self.m.signal_reasons(f, 21, m)[0]["push"], "down")
        m["horizons"]["21"]["platt"] = [-3.0, 0.0]          # the shrink flipped this signal
        self.assertEqual(self.m.signal_reasons(f, 21, m)[0]["push"], "up")
        m["horizons"]["21"]["platt"] = [0.001, 0.0]         # ...or shrank it to nothing
        self.assertEqual(self.m.signal_reasons(f, 21, m), [])

    def test_every_signal_reads_in_plain_english(self):
        for k, say in self.m.SIGNAL_SAYS.items():
            self.assertIn(k, self.m.SIGNAL_FEATURES)
            for v in (-12.3, 0.0, 0.7, 9.0):
                self.assertTrue(say(v)[0].isupper(), k)

    def test_calibration_lookup(self):
        m = tiny_model()
        self.assertEqual(self.m.signal_calibration(0.40, 21, m)["beatRate"], 0.46)
        self.assertEqual(self.m.signal_calibration(0.60, 21, m)["beatRate"], 0.57)
        self.assertEqual(self.m.signal_calibration(0.99, 21, m)["beatRate"], 0.57)

    def test_the_shipped_model_loads_and_scores(self):
        self.m.SIGNAL_FILE = ROOT / "models" / "juno-signal.json"
        m = self.m.signal_model()
        self.assertIsNotNone(m, "models/juno-signal.json ships with FAAM")
        self.assertEqual(m["features"], list(self.m.SIGNAL_FEATURES), "trained on today's signals")
        f = self.m.signal_features(series(300), [1e6] * 300, series(300, seed=9), series(300, seed=4))
        for d in (5, 21, 63):
            self.assertTrue(0 < self.m.signal_score(f, d, m) < 1)


class TestForecast(Case):

    def setUp(self):
        super().setUp()
        days = weekdays(420)
        self.series = {"TEST": series(420, drift=0.006), "SPY": series(420, drift=0.0003, seed=5),
                       "XLK": series(420, drift=0.0005, seed=6)}
        self.m.yahoo_quote = lambda sym, range_="1mo", interval="1d": (
            {"symbol": sym, "name": "Test Co", "history": bars_for(self.series[sym], days)}
            if sym in self.series else {"error": "unknown"})
        self.m.juno_installed = lambda: False

    def test_coming_soon_until_juno_signal_passes(self):
        juno = next(x for x in self.m.forecast_models_public() if x["id"] == "juno")
        self.assertTrue(juno["comingSoon"])
        self.use_model(tiny_model(passed=()))
        self.assertFalse(self.m.juno_status()["forecastReady"])
        self.use_model(tiny_model(passed=(21,)))
        st = self.m.juno_status()
        self.assertEqual((st["forecastReady"], st["passed"], st["signal"]), (True, [21], "JUNO Signal"))
        juno = next(x for x in self.m.forecast_models_public() if x["id"] == "juno")
        self.assertTrue(juno["live"])
        self.assertFalse(juno["comingSoon"])

    def test_not_trained(self):
        r = self.m.compute_forecast("TEST", 30, "juno")
        self.assertIn("isn't trained yet", r["error"])

    def test_a_juno_forecast(self):
        self.use_model(tiny_model())
        r = self.m.compute_forecast("TEST", 30, "juno")
        self.assertNotIn("error", r)
        j = r["juno"]
        self.assertEqual((j["name"], j["signal"], j["days"], j["vs"]), ("Llama X JUNO", "JUNO Signal", 21, "S&P 500"))
        self.assertAlmostEqual(j["probBeat"], 0.6, places=4, msg="it's been beating SPY, so the tree says 60%")
        self.assertEqual(j["history"], {"beatRate": 0.57, "n": 500, "years": "2020–2026"})
        self.assertEqual(j["reasons"][0]["signal"], "ex63")
        self.assertTrue(j["passed"])
        self.assertTrue(j["testing"])
        self.assertNotIn("builtWith", j, "Llama didn't write anything here")
        self.assertGreater(j["expExcessPct"], 0)
        self.assertEqual(len(r["forecast"]), 31)
        self.assertEqual(r["model"]["name"], "Llama X JUNO")

    def test_horizon_maps_to_a_trained_one(self):
        self.use_model(tiny_model())
        self.assertEqual(self.m.compute_forecast("TEST", 7, "juno")["juno"]["days"], 5)
        self.assertEqual(self.m.compute_forecast("TEST", 90, "juno")["juno"]["days"], 63)
        self.assertFalse(self.m.compute_forecast("TEST", 7, "juno")["juno"]["passed"])

    def test_llama_explains_when_installed(self):
        self.use_model(tiny_model())
        self.m._juno_explain_read = lambda name, sym, r: "It has been beating the market."
        j = self.m.compute_forecast("TEST", 30, "juno")["juno"]
        self.assertEqual((j["explanation"], j["explainedBy"], j["builtWith"]),
                         ("It has been beating the market.", "Llama X JUNO", "Built with Llama"))

    def test_too_little_history(self):
        self.use_model(tiny_model())
        self.series["TEST"] = self.series["TEST"][-60:]
        days = weekdays(60)
        quote = self.m.yahoo_quote
        self.m.yahoo_quote = lambda sym, range_="1mo", interval="1d": (
            {"symbol": "TEST", "history": bars_for(self.series["TEST"], days)} if sym == "TEST" else quote(sym))
        self.assertIn("seven months", self.m.compute_forecast("TEST", 30, "juno")["error"])

    def test_other_models_unchanged(self):
        r = self.m.compute_forecast("TEST", 30, "apollo")
        self.assertEqual(r["model"]["id"], "apollo")
        self.assertNotIn("juno", r)


class TestScorecard(Case):
    """JUNO's live record: calls once a day, scored when they come due."""

    def setUp(self):
        super().setUp()
        self.use_model(tiny_model())
        self.days = [d.isoformat() for d in weekdays(400, end=date(2025, 9, 30))]
        self.data = {"SPY": series(400, drift=0.0003, seed=5), "AAA": series(400, drift=0.003, seed=7),
                     "BBB": series(400, drift=-0.002, seed=8), "CCC": series(400, drift=-0.001, seed=9)}
        self.upto = 300
        self.m.SCORECARD_SYMBOLS = ("AAA", "BBB", "CCC")
        self.real_completed_daily = self.m._completed_daily
        self.m._completed_daily = lambda sym, range_="2y": [
            (d, c, 1e6) for d, c in zip(self.days[:self.upto], self.data[sym][:self.upto])]
        self.m._et_now = lambda: datetime.fromisoformat(self.days[self.upto - 1]) + timedelta(hours=20)

    def calls(self):
        return self.m._sc_load()["calls"]

    def test_one_call_per_stock_horizon_and_day(self):
        self.assertEqual(self.m.scorecard_make_calls(), 9)
        self.assertEqual(self.m.scorecard_make_calls(), 0, "already called today")
        c = {(x["sym"], x["days"]): x for x in self.calls()}
        self.assertEqual(c[("AAA", 21)]["start"], self.days[299])
        self.assertAlmostEqual(c[("AAA", 21)]["p"], 0.6, places=4)
        self.assertAlmostEqual(c[("BBB", 21)]["p"], 0.4, places=4)
        self.upto = 301
        self.assertEqual(self.m.scorecard_make_calls(), 9, "a new session, new calls")

    def test_calls_are_scored_when_due(self):
        self.m.scorecard_make_calls()
        self.assertEqual(self.m.scorecard_resolve(), 0, "nothing due yet")
        self.upto = 380
        self.assertEqual(self.m.scorecard_resolve(), 9)
        c = {(x["sym"], x["days"]): x for x in self.calls()}
        a, spy = self.data["AAA"], self.data["SPY"]
        want = ((a[320] / a[299]) - (spy[320] / spy[299])) * 100
        self.assertAlmostEqual(c[("AAA", 21)]["result"]["excessPct"], round(want, 3), places=3)
        self.assertEqual(c[("AAA", 21)]["result"]["end"], self.days[320])
        self.assertEqual(c[("AAA", 21)]["result"]["beat"], want > 0)
        self.assertEqual(self.m.scorecard_resolve(), 0, "scored once")

    def test_summary(self):
        self.m.scorecard_make_calls()
        s = self.m.scorecard_summary()
        self.assertEqual((s["live"]["21"]["made"], s["live"]["21"]["scored"], s["live"]["21"]["pending"]), (3, 0, 3))
        self.assertEqual(s["since"], self.days[299])
        self.assertEqual(s["symbols"], ["AAA", "BBB", "CCC"], "a fixed list, never anyone's watchlist")
        self.upto = 380
        self.m.scorecard_resolve()
        live = self.m.scorecard_summary()["live"]["21"]
        self.assertEqual(live["scored"], 3)
        self.assertFalse(live["enough"])
        self.assertIn("hitRate", live)
        self.assertEqual(self.m.scorecard_summary()["backtest"]["21"]["passed"], True)
        self.assertTrue(all(set(x) >= {"sym", "days", "start", "p", "due"} for x in s["recent"]))

    def test_no_model_no_calls(self):
        self.m.SIGNAL_FILE = Path(self.home) / "gone.json"
        self.assertEqual(self.m.scorecard_make_calls(), 0)

    def test_half_finished_day_is_ignored(self):
        days = weekdays(5)
        self.m.yahoo_quote = lambda sym, range_="1mo", interval="1d": {"history": bars_for([1, 2, 3, 4, 5], days)}
        self.m._completed_daily = self.real_completed_daily
        last = days[-1]
        self.m._et_now = lambda: datetime(last.year, last.month, last.day, 11, 0)
        self.assertEqual(len(self.m._completed_daily("X")), 4, "still trading: today's bar isn't final")
        self.m._et_now = lambda: datetime(last.year, last.month, last.day, 16, 30)
        self.assertEqual(len(self.m._completed_daily("X")), 5)

    def test_over_http(self):
        self.m.scorecard_make_calls()
        httpd = self.m.ThreadingHTTPServer(("127.0.0.1", 0), self.m.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{httpd.server_address[1]}/api/juno/scorecard"
            with urllib.request.urlopen(url, timeout=10) as r:
                d = json.loads(r.read())
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual(d["live"]["5"]["made"], 3)
        self.assertTrue(d["testing"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
