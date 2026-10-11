"""Score Llama X JUNO on held-out windows from mid-2024 onward (after Llama's
knowledge cutoff, and never seen in training), against honest baselines.

    ~/.faam/juno/venv/bin/python juno/evaluate.py            # the fine-tuned JUNO
    ~/.faam/juno/venv/bin/python juno/evaluate.py --base     # plain Llama, for comparison
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime import BANDS, HOME, Juno  # noqa: E402


def spearman(xs, ys):
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        for k, i in enumerate(order):
            r[i] = k
        return r
    rx, ry = ranks(xs), ranks(ys)
    n = len(xs)
    mx_, my_ = sum(rx) / n, sum(ry) / n
    cov = sum((rx[i] - mx_) * (ry[i] - my_) for i in range(n))
    sx = math.sqrt(sum((x - mx_) ** 2 for x in rx)) or 1
    sy = math.sqrt(sum((y - my_) ** 2 for y in ry)) or 1
    return cov / (sx * sy)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", action="store_true", help="score plain Llama without JUNO's training")
    ap.add_argument("--n", type=int, default=0, help="only the first N test windows")
    args = ap.parse_args()
    rows = [json.loads(line) for line in open(HOME / "data" / "eval.jsonl")]
    rows = rows[: args.n] if args.n else rows
    juno = Juno(adapters=not args.base)
    meta = juno.meta
    prior, mz = meta["bandPrior"], meta["bandMeanZ"]
    prior_band = max(prior, key=prior.get)

    hit = dir_hit = dir_n = mom_hit = up_days = 0
    brier = brier_prior = ll = ll_prior = 0.0
    exp_ret, real_ret = [], []
    t0 = time.time()
    for k, r in enumerate(rows, 1):
        m = r["meta"]
        p = juno.band_probs(r["messages"][1]["content"])
        true, ret = m["band"], m["ret"]
        up = 1.0 if ret > 0 else 0.0
        up_days += up
        hit += max(p, key=p.get) == true
        p_up = p["D"] + p["E"] + 0.5 * p["C"]
        brier += (p_up - up) ** 2
        brier_prior += ((prior["D"] + prior["E"] + 0.5 * prior["C"]) - up) ** 2
        ll += -math.log(max(p[true], 1e-9))
        ll_prior += -math.log(prior[true])
        if abs((p["D"] + p["E"]) - (p["A"] + p["B"])) > 1e-9:
            dir_n += 1
            dir_hit += ((p["D"] + p["E"]) > (p["A"] + p["B"])) == (ret > 0)
        one_month = re.search(r"1m ([+-]\d+\.\d)%", r["messages"][1]["content"])
        mom_hit += ((float(one_month.group(1)) if one_month else 0) > 0) == (ret > 0)
        z = sum(p[b] * mz[b] for b in BANDS)
        exp_ret.append(z)
        real_ret.append(ret / (m["sigma"] * 100 * math.sqrt(m["days"])))
        if k % 50 == 0:
            print(f"  {k}/{len(rows)}  {time.time() - t0:.0f}s", flush=True)
    n = len(rows)
    report = {
        "model": f"{meta['name']} ({'base Llama, untuned' if args.base else 'fine-tuned'})",
        "tested": n, "window": f"{meta['splits']['testStart']} onward", "seconds": round(time.time() - t0),
        "bandAccuracy": round(hit / n, 3), "bandAccuracyBaseline": round(
            sum(r["meta"]["band"] == prior_band for r in rows) / n, 3),
        "directionAccuracy": round(dir_hit / max(1, dir_n), 3),
        "directionBaselineAlwaysUp": round(up_days / n, 3),
        "directionBaselineMomentum": round(mom_hit / n, 3),
        "brierUp": round(brier / n, 4), "brierUpBaseline": round(brier_prior / n, 4),
        "logLoss": round(ll / n, 4), "logLossBaseline": round(ll_prior / n, 4),
        "rankCorrelation": round(spearman(exp_ret, real_ret), 3),
    }
    out = HOME / ("eval-base.json" if args.base else "eval-juno.json")
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
