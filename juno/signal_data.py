"""Download the data JUNO Signal learns from.

JUNO Signal is JUNO's numbers model: it reads a stock's recent behaviour and
gives the odds it beats the S&P 500 over the next 5, 21 or 63 trading days.

  universe  today's S&P 500 members (Wikipedia), with their sector and the date
            each joined the index. Training only uses a stock from the day it
            joined, so the run-up that got it into the index never counts.
  prices    10 years of daily dividend-adjusted closes and volume (Yahoo) for
            every member, SPY, and the 11 sector ETFs.

Usage:  /usr/bin/python3 juno/signal_data.py      (macOS's Python has working certificates)
Writes  ~/.faam/juno/signal/universe.json and signal/raw/<SYMBOL>.json
"""
from __future__ import annotations

import html
import json
import os
import re
import ssl
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HOME = Path(os.environ.get("JUNO_HOME", Path.home() / ".faam" / "juno")) / "signal"
RAW = HOME / "raw"
WIKI = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
UA = {"User-Agent": "Mozilla/5.0 (FAAM JUNO research)"}

# GICS sector -> the SPDR ETF that tracks it.
SECTOR_ETF = {
    "Communication Services": "XLC", "Consumer Discretionary": "XLY", "Consumer Staples": "XLP",
    "Energy": "XLE", "Financials": "XLF", "Health Care": "XLV", "Industrials": "XLI",
    "Information Technology": "XLK", "Materials": "XLB", "Real Estate": "XLRE", "Utilities": "XLU",
}
MARKET = "SPY"


def _ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _get(url: str, timeout: float = 30) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as r:
        return r.read()


def universe() -> list:
    """[{symbol, name, sector, added}] for today's S&P 500."""
    page = _get(WIKI).decode("utf-8", "replace")
    i = page.find('id="constituents"')
    table = page[i: page.find("</table>", i)]
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S):
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) >= 6 and cells[2] in SECTOR_ETF:
            added = cells[5][:10] if re.match(r"\d{4}-\d\d-\d\d", cells[5]) else ""
            out.append({"symbol": cells[0].replace(".", "-"), "name": cells[1],
                        "sector": cells[2], "added": added})
    if len(out) < 450:
        raise SystemExit(f"Only found {len(out)} S&P 500 members; the Wikipedia page may have changed.")
    return out


def history(sym: str, tries: int = 4) -> list:
    """[{d, c, v}] daily, dividend-adjusted; cached for a day."""
    cache = RAW / f"{sym}.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 86400:
        return json.loads(cache.read_text())
    url = (f"https://query2.finance.yahoo.com/v8/finance/chart/{sym}"
           "?range=10y&interval=1d&includeAdjustedClose=true")
    for k in range(tries):
        try:
            res = json.loads(_get(url))["chart"]["result"][0]
            break
        except Exception as e:  # noqa: BLE001
            if k == tries - 1:
                print(f"  {sym}: {e}", file=sys.stderr)
                return []
            time.sleep(2 * (k + 1))
    ts = res.get("timestamp") or []
    q = res["indicators"]["quote"][0]
    adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q["close"]
    bars = []
    for t, c, v in zip(ts, adj, q.get("volume") or [0] * len(ts)):
        if c and c > 0:
            bars.append({"d": time.strftime("%Y-%m-%d", time.gmtime(t)), "c": round(c, 6), "v": v or 0})
    cache.write_text(json.dumps(bars))
    return bars


def main() -> int:
    RAW.mkdir(parents=True, exist_ok=True)
    members = universe()
    (HOME / "universe.json").write_text(json.dumps(
        {"fetched": time.strftime("%Y-%m-%d"), "market": MARKET, "sectorEtf": SECTOR_ETF,
         "members": members}, indent=1))
    symbols = [MARKET, *SECTOR_ETF.values(), *[m["symbol"] for m in members]]
    print(f"{len(members)} S&P 500 members; downloading {len(symbols)} price histories…", flush=True)
    done = ok = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        for bars in pool.map(history, symbols):
            done += 1
            ok += bool(bars)
            if done % 50 == 0:
                print(f"  {done}/{len(symbols)}", flush=True)
    print(f"Done: {ok}/{len(symbols)} histories in {RAW}", flush=True)
    return 0 if ok > 0.9 * len(symbols) else 1


if __name__ == "__main__":
    sys.exit(main())
