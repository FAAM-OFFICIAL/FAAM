"""Build Llama X JUNO's training data.

Two skills:
  forecast — from a stock's recent behaviour (no ticker, no date), pick the band
             its return lands in over the next 5 / 21 / 63 trading days.
  explain  — FAAM Learn lessons and Titan's answers, so JUNO explains stocks the
             way FAAM does.

Splits are by date so the test set is genuinely unseen:
  train  windows ending before 2023-07-01
  valid  2023-07-01 .. 2024-06-01
  test   2024-07-01 onward (after Llama 3.1's knowledge cutoff)

Usage:  ~/.faam/juno/venv/bin/python juno/build_data.py
Writes  ~/.faam/juno/data/{train,valid,eval}.jsonl and meta.json
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import random
import ssl
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = Path(os.environ.get("JUNO_HOME", Path.home() / ".faam" / "juno")) / "data"
RAW = OUT / "raw"

UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "BRK-B", "JPM", "V", "MA", "UNH",
    "JNJ", "PG", "XOM", "CVX", "HD", "KO", "PEP", "MRK", "ABBV", "COST", "WMT", "BAC", "DIS",
    "NFLX", "ADBE", "CRM", "ORCL", "INTC", "AMD", "CSCO", "QCOM", "TXN", "NKE", "MCD", "BA",
    "CAT", "GE", "IBM", "GS", "SBUX", "PFE", "T", "VZ",
    "QQQ", "IWM", "DIA", "XLF", "XLE", "XLK", "XLV", "XLY", "GLD", "TLT",
]
MARKET = "SPY"
HORIZON_WEIGHTS = {5: 0.3, 21: 0.45, 63: 0.25}
TRAIN_END, VALID_END, TEST_START = "2023-07-01", "2024-06-01", "2024-07-01"
N_TRAIN, N_VALID, N_TEST = 2400, 240, 600
STEP = 5


def load_faam():
    os.environ.setdefault("FAAM_DATA_DIR", tempfile.mkdtemp(prefix="juno-faam-"))
    os.environ["OPENAI_API_KEY"] = ""
    spec = importlib.util.spec_from_file_location("faam_app", HERE.parent / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ssl():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def history(sym: str) -> list:
    """10 years of daily bars [{d, c (adjusted), v}], cached under data/raw."""
    RAW.mkdir(parents=True, exist_ok=True)
    cache = RAW / f"{sym}.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 86400:
        return json.loads(cache.read_text())
    url = (f"https://query2.finance.yahoo.com/v8/finance/chart/{sym}"
           "?range=10y&interval=1d&includeAdjustedClose=true")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (FAAM JUNO)"})
    with urllib.request.urlopen(req, timeout=30, context=_ssl()) as r:
        res = json.loads(r.read())["chart"]["result"][0]
    ts = res.get("timestamp") or []
    q = res["indicators"]["quote"][0]
    adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q["close"]
    bars = []
    for i, t in enumerate(ts):
        c = adj[i] if i < len(adj) else None
        if c:
            bars.append({"d": datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"),
                         "c": round(c, 4), "v": (q.get("volume") or [None] * len(ts))[i] or 0})
    cache.write_text(json.dumps(bars))
    return bars


def forecast_examples(faam, sym, bars, market_by_day, rng):
    out = []
    closes = [b["c"] for b in bars]
    vols = [b["v"] for b in bars]
    for i in range(260, len(bars) - 1, STEP):
        days = rng.choices(list(HORIZON_WEIGHTS), weights=list(HORIZON_WEIGHTS.values()))[0]
        j = i + days
        if j >= len(bars):
            continue
        d, d_end = bars[i]["d"], bars[j]["d"]
        split = ("train" if d_end < TRAIN_END else
                 "valid" if TRAIN_END <= d and d_end < VALID_END else
                 "test" if d >= TEST_START else None)
        if not split:
            continue                                  # label window straddles a split
        mi = market_by_day.get(d)
        f = faam.juno_features(closes[: i + 1], vols[: i + 1], mi)
        if not f or f["sigma"] <= 0:
            continue
        ret = (closes[j] / closes[i] - 1.0) * 100.0
        band = faam.juno_band(ret, f["sigma"], days)
        out.append({"split": split, "sym": sym, "date": d, "days": days, "ret": round(ret, 3),
                    "sigma": f["sigma"], "band": band,
                    "z": math.log(1 + ret / 100.0) / (f["sigma"] * math.sqrt(days)),
                    "prompt": faam.juno_prompt(f, days)})
    return out


def chat(system, user, answer):
    return {"messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user},
                         {"role": "assistant", "content": answer}]}


EXPLAIN_SYSTEM = ("You are Llama X JUNO, FAAM's stock model. Explain investing ideas clearly and "
                  "briefly for everyday investors, in plain words. This is information, not "
                  "financial advice.")


def explain_examples(faam) -> list:
    ex = []
    for course in faam.LEARN_LIBRARY:
        for l in course.get("lessons", []):
            ex.append(chat(EXPLAIN_SYSTEM, f"Explain this: {l['t']}", l["b"]))
            opts = "\n".join(f"- {o}" for o in l["o"])
            ex.append(chat(EXPLAIN_SYSTEM, f"{l['q']}\n{opts}",
                           f"{l['o'][l['c']]}. {l['b']}"))
    for item in getattr(faam, "TITAN_SEED", []):
        if item.get("q") and item.get("a"):
            ex.append(chat(EXPLAIN_SYSTEM, item["q"][0].upper() + item["q"][1:] + "?"
                           if not item["q"].endswith("?") else item["q"], item["a"]))
    return ex


def main():
    rng = random.Random(7)
    faam = load_faam()
    print(f"Fetching {len(UNIVERSE) + 1} histories…")
    mbars = history(MARKET)
    mcloses = [b["c"] for b in mbars]
    market_by_day = {b["d"]: mcloses[: k + 1][-70:] for k, b in enumerate(mbars)}
    rows = []
    for sym in UNIVERSE:
        try:
            bars = history(sym)
        except Exception as e:  # noqa: BLE001
            print(f"  skip {sym}: {e}")
            continue
        got = forecast_examples(faam, sym, bars, market_by_day, rng)
        rows += got
        print(f"  {sym:6} {len(bars):5} days  {len(got):4} windows")
        time.sleep(0.2)
    by = {s: [r for r in rows if r["split"] == s] for s in ("train", "valid", "test")}
    for s in by:
        rng.shuffle(by[s])
    by["train"], by["valid"], by["test"] = by["train"][:N_TRAIN], by["valid"][:N_VALID], by["test"][:N_TEST]

    # Average move (in units of the stock's own volatility) inside each band, from
    # training data only: turns band probabilities into an expected % return.
    mean_z = {b: round(sum(r["z"] for r in by["train"] if r["band"] == b) /
                       max(1, sum(1 for r in by["train"] if r["band"] == b)), 4)
              for b in faam.JUNO_BANDS}
    prior = {b: round(sum(1 for r in by["train"] if r["band"] == b) / len(by["train"]), 4)
             for b in faam.JUNO_BANDS}

    explain = explain_examples(faam)
    rng.shuffle(explain)
    cut = max(1, len(explain) // 10)
    ex_valid, ex_train = explain[:cut], explain[cut:] * 2       # upweight the small set

    def fc(r):
        return chat(faam.JUNO_SYSTEM, r["prompt"], json.dumps({"band": r["band"]}))

    OUT.mkdir(parents=True, exist_ok=True)
    train = [fc(r) for r in by["train"]] + ex_train
    valid = [fc(r) for r in by["valid"]] + ex_valid
    rng.shuffle(train)
    rng.shuffle(valid)
    for name, data in (("train", train), ("valid", valid)):
        with open(OUT / f"{name}.jsonl", "w") as fh:
            for row in data:
                fh.write(json.dumps(row) + "\n")
    with open(OUT / "eval.jsonl", "w") as fh:     # held-out test with answers, for evaluate.py
        for r in by["test"]:
            fh.write(json.dumps({**fc(r), "meta": {k: r[k] for k in ("sym", "date", "days", "ret", "sigma", "band")}}) + "\n")
    meta = {"name": faam.JUNO_NAME, "base": faam.JUNO_BASE, "built": datetime.now().isoformat(timespec="seconds"),
            "universe": UNIVERSE, "market": MARKET, "splits": {"trainEnd": TRAIN_END, "validEnd": VALID_END,
            "testStart": TEST_START}, "counts": {"forecastTrain": len(by["train"]), "forecastValid": len(by["valid"]),
            "test": len(by["test"]), "explainTrain": len(ex_train), "explainValid": len(ex_valid)},
            "bandMeanZ": mean_z, "bandPrior": prior,
            "forecastSystem": faam.JUNO_SYSTEM, "explainSystem": EXPLAIN_SYSTEM}
    (OUT / "meta.json").write_text(json.dumps(meta, indent=2))
    print(json.dumps(meta["counts"]), "\nband prior", prior, "\nband mean z", mean_z)


if __name__ == "__main__":
    sys.exit(main())
