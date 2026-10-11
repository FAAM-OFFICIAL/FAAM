"""Train and test JUNO Signal, JUNO's numbers model.

Question: will this stock beat the S&P 500 over the next 5, 21 or 63 trading
days? Asking "will it go up?" mostly asks where the whole market goes, which
nobody predicts well. Beating the market strips that out, and it's where the
known effects (momentum, short-term reversal, low volatility) live.

Honest testing (walk-forward): each test year is predicted by a model trained
only on earlier data, and a training example only counts once its outcome was
known before that year started. Nothing from the test year leaks in.

Attempt 2 (after attempt 1 was overconfident): inside every training window
the last year is held out to pick how many trees to keep and to shrink the
odds toward 50/50 (Platt scaling), then the model is refit on the whole window.
The test year is never used for either choice.

The bar, set before looking at results. A horizon passes only if, out of sample:
  1. log loss beats always quoting the training base rate, and
  2. the average daily rank correlation between JUNO's odds and what happened
     is above zero with t >= 2, counted on non-overlapping dates.

Usage:  ~/.faam/juno/venv/bin/python juno/signal_train.py
Needs   juno/signal_data.py run first (S&P 500 prices in ~/.faam/juno/signal/raw)
Writes  models/juno-signal.json (what FAAM loads) and ~/.faam/juno/eval-signal.json
"""
from __future__ import annotations

import argparse
import bisect
import importlib.util
import json
import math
import os
import pickle
import sys
import tempfile
import time
from datetime import date
from multiprocessing import Pool
from pathlib import Path

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss, roc_auc_score

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("JUNO_HOME", Path.home() / ".faam" / "juno"))
SIG = HOME / "signal"
OUT_MODEL = HERE.parent / "models" / "juno-signal.json"
OUT_EVAL = HOME / "eval-signal.json"
HORIZONS = (5, 21, 63)
STEP = 5                      # one example per stock every 5 trading days
MIN_HISTORY = 260             # bars before the first example (a year of signals)
FIRST_TEST_YEAR = 2020
PARAMS = dict(max_iter=400, learning_rate=0.03, max_leaf_nodes=15, min_samples_leaf=400,
              l2_regularization=1.0, early_stopping=False, random_state=0)
ATTEMPT = 2

faam = None


def load_faam():
    os.environ.setdefault("FAAM_DATA_DIR", tempfile.mkdtemp(prefix="juno-signal-"))
    os.environ["OPENAI_API_KEY"] = ""
    spec = importlib.util.spec_from_file_location("faam_app", HERE.parent / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def bars(sym: str) -> list:
    p = SIG / "raw" / f"{sym}.json"
    return json.loads(p.read_text()) if p.exists() else []


# ---- dataset -----------------------------------------------------------------

def _examples(job):
    """Every 5th trading day for one stock, from the day it joined the S&P 500."""
    sym, etf, added, grid = job
    global faam
    if faam is None:
        faam = load_faam()
    b = bars(sym)
    if len(b) < MIN_HISTORY + 10:
        return []
    mk = {x["d"]: x["c"] for x in bars("SPY")}
    sc = {x["d"]: x["c"] for x in bars(etf)} if etf else {}
    b = [x for x in b if x["d"] in mk]
    days = [x["d"] for x in b]
    c = [x["c"] for x in b]
    v = [x["v"] for x in b]
    m = [mk[d] for d in days]
    s = [sc.get(d) for d in days]
    on_grid = set(grid)
    rows = []
    for i in range(MIN_HISTORY, len(b)):
        d = days[i]
        if d not in on_grid or (added and d < added):
            continue
        lo = max(0, i - 299)
        sec = s[lo:i + 1] if all(s[lo:i + 1]) else None
        f = faam.signal_features(c[lo:i + 1], v[lo:i + 1], m[lo:i + 1], sec)
        if not f:
            continue
        labels = {}
        for h in HORIZONS:
            if i + h < len(b):
                labels[h] = ((c[i + h] / c[i]) - (m[i + h] / m[i])) * 100, days[i + h]
        rows.append((d, sym, [f[k] for k in faam.SIGNAL_FEATURES], f["sigma"], labels))
    return rows


def build_dataset():
    """All examples, cached in signal/dataset.pkl until the prices or app.py change."""
    uni = json.loads((SIG / "universe.json").read_text())
    cache = SIG / "dataset.pkl"
    newest = max([p.stat().st_mtime for p in (SIG / "raw").glob("*.json")]
                 + [(HERE.parent / "app.py").stat().st_mtime])
    if cache.exists() and cache.stat().st_mtime > newest:
        with cache.open("rb") as fh:
            rows = pickle.load(fh)
        print(f"{len(rows):,} examples (cached)", flush=True)
        return rows, uni
    spy = [x["d"] for x in bars("SPY")]
    grid = spy[MIN_HISTORY::STEP]
    jobs = [(mbr["symbol"], uni["sectorEtf"].get(mbr["sector"]), mbr.get("added", ""), grid)
            for mbr in uni["members"]]
    t0 = time.time()
    with Pool(max(1, (os.cpu_count() or 2) - 1)) as pool:
        parts = pool.map(_examples, jobs, chunksize=4)
    rows = [r for p in parts for r in p]
    print(f"{len(rows):,} examples from {sum(1 for p in parts if p)} stocks in {time.time() - t0:.0f}s", flush=True)
    with cache.open("wb") as fh:
        pickle.dump(rows, fh)
    return rows, uni


# ---- metrics -----------------------------------------------------------------

def _rank(a):
    r = np.empty(len(a))
    r[np.argsort(a, kind="mergesort")] = np.arange(len(a))
    return r


def daily_ic(dates, score, excess, h):
    """Mean daily Spearman correlation, its t-stat on non-overlapping dates, and
    the average gap between the top and bottom fifth of each day's ranking."""
    by = {}
    for i, d in enumerate(dates):
        by.setdefault(d, []).append(i)
    days = sorted(by)
    ics, spreads = [], []
    for d in days:
        idx = by[d]
        if len(idx) < 20:
            ics.append(None), spreads.append(None)
            continue
        s, e = score[idx], excess[idx]
        ics.append(float(np.corrcoef(_rank(s), _rank(e))[0, 1]))
        q = max(1, len(idx) // 5)
        o = np.argsort(s)
        spreads.append(float(e[o[-q:]].mean() - e[o[:q]].mean()))
    gap = math.ceil(h / STEP)
    sample = [x for x in ics[::gap] if x is not None]
    valid = [x for x in ics if x is not None]
    mean = float(np.mean(valid)) if valid else 0.0
    t = (float(np.mean(sample)) / (float(np.std(sample, ddof=1)) / math.sqrt(len(sample)))
         if len(sample) > 2 and np.std(sample) > 0 else 0.0)
    sp = [x for x in spreads if x is not None]
    return {"ic": round(mean, 4), "icT": round(t, 2), "icDates": len(sample),
            "topMinusBottomPct": round(float(np.mean(sp)), 3) if sp else 0.0}


def _shift_year(d: str, years: int = -1) -> str:
    return f"{int(d[:4]) + years}{d[4:]}"


def fit_calibrated(X, y, dates, end):
    """Fit on a window, using its last year to choose the number of trees and a
    Platt shrink; then refit on the whole window with that many trees.
    Returns (model, (a, b), trees)."""
    cut = _shift_year(max(dates.tolist()))
    inner, val = end < cut, dates >= cut
    probe = HistGradientBoostingClassifier(**PARAMS).fit(X[inner], y[inner])
    losses = [log_loss(y[val], p[:, 1], labels=[0, 1]) for p in probe.staged_predict_proba(X[val])]
    n = int(np.argmin(losses)) + 1
    raw = list(probe.staged_decision_function(X[val]))[n - 1].reshape(-1, 1)
    platt = LogisticRegression(C=1e6).fit(raw, y[val])
    a, b = float(platt.coef_[0][0]), float(platt.intercept_[0])
    model = HistGradientBoostingClassifier(**{**PARAMS, "max_iter": n}).fit(X, y)
    return model, (a, b), n


def calibrated(model, ab, X):
    a, b = ab
    return 1 / (1 + np.exp(-(a * model.decision_function(X) + b)))


def export_trees(clf, names):
    trees = []
    for (pred,) in clf._predictors:
        nodes = []
        for nd in pred.nodes:
            if nd["is_leaf"]:
                nodes.append([-1, round(float(nd["value"]), 7)])
            else:
                nodes.append([int(nd["feature_idx"]), float(nd["num_threshold"]),
                              int(nd["left"]), int(nd["right"]), int(nd["missing_go_to_left"])])
        trees.append(nodes)
    return {"baseline": float(np.ravel(clf._baseline_prediction)[0]), "trees": trees}


def main(argv=None) -> int:
    global faam
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--first-test-year", type=int, default=FIRST_TEST_YEAR)
    args = ap.parse_args(argv)
    faam = load_faam()
    rows, uni = build_dataset()
    names = list(faam.SIGNAL_FEATURES)
    X = np.array([[np.nan if x is None else x for x in r[2]] for r in rows], dtype=np.float64)
    dates = np.array([r[0] for r in rows])
    sigma = np.array([r[3] for r in rows])
    mom = X[:, names.index("mom12_1")]
    model = {"name": faam.SIGNAL_NAME, "version": ATTEMPT, "trained": date.today().isoformat(),
             "question": "Odds the stock beats the S&P 500 (SPY) over the next N trading days",
             "universe": f"S&P 500 members ({len(uni['members'])}), each from the day it joined the index",
             "dataFrom": min(dates.tolist()), "dataTo": max(dates.tolist()),
             "features": names, "params": PARAMS,
             "medians": {k: (None if np.isnan(v) else round(float(v), 5))
                         for k, v in zip(names, np.nanmedian(X, axis=0))},
             "sectorEtf": {mbr["symbol"]: uni["sectorEtf"][mbr["sector"]] for mbr in uni["members"]},
             "horizons": {}}
    report = {"attempt": ATTEMPT,
              "bar": "log loss below the base rate AND mean daily rank correlation > 0 with t >= 2",
              "horizons": {}}

    for h in HORIZONS:
        have = np.array([h in r[4] for r in rows])
        Xh, dh, sh, mh = X[have], dates[have], sigma[have], mom[have]
        ex = np.array([r[4][h][0] for r in rows if h in r[4]])
        end = np.array([r[4][h][1] for r in rows if h in r[4]])
        y = (ex > 0).astype(int)
        years = sorted({int(d[:4]) for d in dh if int(d[:4]) >= args.first_test_year})
        oos_p, oos_base, oos_idx, per_year = [], [], [], []
        for yr in years:
            start = f"{yr}-01-01"
            tr = end < start
            te = (dh >= start) & (dh < f"{yr + 1}-01-01")
            if tr.sum() < 20000 or te.sum() < 1000:
                continue
            clf, ab, n_trees = fit_calibrated(Xh[tr], y[tr], dh[tr], end[tr])
            p = calibrated(clf, ab, Xh[te])
            base = float(y[tr].mean())
            oos_p.append(p), oos_base.append(np.full(te.sum(), base)), oos_idx.append(np.where(te)[0])
            yi = daily_ic(dh[te], p, ex[te], h)
            per_year.append({"year": yr, "n": int(te.sum()), "ic": yi["ic"], "trees": n_trees,
                             "logLoss": round(log_loss(y[te], p, labels=[0, 1]), 4),
                             "baseLogLoss": round(log_loss(y[te], np.full(te.sum(), base), labels=[0, 1]), 4)})
            print(f"  {h:>2}d {yr}: n={te.sum():,} trees={n_trees} ic={yi['ic']:+.4f} "
                  f"logloss {per_year[-1]['logLoss']:.4f} vs {per_year[-1]['baseLogLoss']:.4f}", flush=True)
        p = np.concatenate(oos_p)
        base = np.concatenate(oos_base)
        idx = np.concatenate(oos_idx)
        yy, ee, dd = y[idx], ex[idx], dh[idx]
        ic = daily_ic(dd, p, ee, h)
        mom_ok = ~np.isnan(mh[idx])
        mom_ic = daily_ic(dd[mom_ok], mh[idx][mom_ok], ee[mom_ok], h)
        ll, bll = log_loss(yy, p, labels=[0, 1]), log_loss(yy, base, labels=[0, 1])
        passed = bool(ll < bll and ic["ic"] > 0 and ic["icT"] >= 2)
        # What the odds have meant: in each tenth of JUNO's odds, how often stocks
        # beat the market and by how much (in units of their own typical move).
        z = ee / 100 / (sh[idx] * math.sqrt(h))
        cuts = np.quantile(p, np.linspace(0, 1, 11))
        calib = []
        for k in range(10):
            sel = (p >= cuts[k]) & (p <= cuts[k + 1]) if k == 9 else (p >= cuts[k]) & (p < cuts[k + 1])
            if sel.sum():
                calib.append([round(float(cuts[k + 1]), 4), round(float(yy[sel].mean()), 4),
                              round(float(np.median(z[sel])), 4), int(sel.sum())])
        ev = {"tested": int(len(p)), "years": f"{years[0]}–{years[-1]}",
              "logLoss": round(ll, 4), "baseLogLoss": round(bll, 4),
              "auc": round(roc_auc_score(yy, p), 4), "beatRate": round(float(yy.mean()), 4),
              "hitRate": round(float(((p >= 0.5) == (yy == 1)).mean()), 4),
              **ic, "momentumIc": mom_ic["ic"], "momentumIcT": mom_ic["icT"],
              "passed": passed, "perYear": per_year}
        report["horizons"][str(h)] = ev
        print(f"{h:>2}d: tested {len(p):,} | log loss {ll:.4f} vs base {bll:.4f} | IC {ic['ic']:+.4f} "
              f"(t {ic['icT']:.1f}) | 12-1 momentum IC {mom_ic['ic']:+.4f} | top-bottom fifth "
              f"{ic['topMinusBottomPct']:+.2f} pts | {'PASS' if passed else 'fail'}", flush=True)
        final, ab, n_trees = fit_calibrated(Xh, y, dh, end)
        model["horizons"][str(h)] = {**export_trees(final, names), "platt": [round(ab[0], 6), round(ab[1], 6)],
                                     "calib": calib,
                                     "eval": {k: v for k, v in ev.items() if k != "perYear"}}

    OUT_MODEL.parent.mkdir(parents=True, exist_ok=True)
    OUT_MODEL.write_text(json.dumps(model, separators=(",", ":")))
    OUT_EVAL.write_text(json.dumps(report, indent=1))
    print(f"Wrote {OUT_MODEL} ({OUT_MODEL.stat().st_size / 1e6:.1f} MB) and {OUT_EVAL}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
