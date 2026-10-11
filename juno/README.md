# Llama X JUNO

FAAM's own stock model, in two parts:

- **JUNO Signal**, a small numbers model that gives the odds a stock beats the
  S&P 500 (see "JUNO Signal" below).
- **Llama X JUNO**, Meta's Llama 3.1 8B Instruct fine-tuned by FAAM on ten
  years of daily stock data and FAAM's own teaching material. It runs on the
  Mac (Apple silicon) and explains things in plain English.

Together they make the **Llama X JUNO** forecast model in FAAM, which is in
heavy testing.

> **Built with Llama.** Llama 3.1 is licensed under the
> [Llama 3.1 Community License](LICENSE-LLAMA.txt), Copyright © Meta Platforms, Inc.
> All Rights Reserved. The license requires any fine-tuned model that's made
> available to have a name that starts with "Llama", and requires showing
> "Built with Llama". That's why the model is called **Llama X JUNO**, and why
> FAAM shows "Built with Llama" wherever JUNO appears. Keep both if you rename
> anything.

## Status: in heavy testing

JUNO × FAAM ships labelled **In heavy testing**. It switches on in a FAAM only
where `install.sh` has run, and FAAM only runs that after JUNO beats the simple
baselines. That means a lower log loss than always quoting the historical band
odds, and a rank correlation above zero. The first full run (600 steps, tested
on 600 unseen cases from July 2024 on) didn't pass yet:

| Test | Plain Llama | Llama X JUNO | Baseline |
|------|-------------|--------------|----------|
| Right band (of 5) | 24.3% | 26.2% | 26.2% |
| Right direction | 54.0% | 56.0% | 56.0% (always "up") |
| Log loss (lower is better) | 1.875 | 1.583 | 1.573 |
| Rank correlation | −0.05 | −0.01 | 0 |

Training taught it the base rates; plain Llama is worse than guessing. It
doesn't have a forecasting edge yet.

## JUNO Signal: the numbers model

Language models are weak at reading numbers, so JUNO now splits the job:

- **JUNO Signal** does the predicting. It's a small gradient-boosted tree
  model (scikit-learn) that gives the **odds a stock beats the S&P 500** over
  the next 5, 21 or 63 trading days. FAAM scores its trees in plain Python
  (`app.py`, `signal_score`), so it runs anywhere FAAM does, including Render.
  The trained model ships in the repo as `models/juno-signal.json` (it isn't
  built from Llama, so it can).
- **Llama X JUNO** only explains the result in plain English, and only on a
  Mac where it's installed. Wherever its words appear, FAAM shows "Built with
  Llama".

**Why "beat the S&P 500" and not "go up".** Whether a stock rises over a month
mostly depends on the whole market, which nobody predicts well. Comparing it
with the market strips that out, and it's where the known effects live
(momentum, short-term reversal, low volatility).

**What it reads (36 signals, `SIGNAL_FEATURES`).** Returns from a week to a
year, 12-month momentum without the last month, volatility, biggest daily
moves, distance from moving averages and the 52-week high and low, RSI, trend
steadiness, volume and dollar volume, performance against the S&P 500, beta,
correlation and stock-specific volatility, the market's own trend and
swings, and its sector ETF against the market and the stock against its
sector. `signal_features()` in `app.py` computes them, and training calls that
same function, so live and training inputs can't drift apart.

**Data.** Today's 503 S&P 500 members (Wikipedia), 10 years of daily
dividend-adjusted prices (Yahoo), SPY and the 11 SPDR sector ETFs: about
193,000 examples, one per stock every 5 trading days. Each stock only counts
from the day it **joined the index**, so the run-up that got it in never
counts. Companies that left the index are still missing, so some
survivorship bias remains.

**Walk-forward test.** Each year from 2020 to 2026 is predicted by a model
trained only on examples whose outcome was known before that year began.

**The bar** (set before the first run). A horizon passes only if, out of sample:
1. its log loss beats always quoting the training base rate, **and**
2. the average daily rank correlation between its odds and what happened is
   above zero with t ≥ 2, counted on non-overlapping dates.

**Results (about 150,000 test calls per horizon).** Nothing has passed yet:

| Horizon | Attempt | Log loss (base) | Rank corr. (t) | Top fifth minus bottom fifth | Right | Passed |
|---------|---------|-----------------|----------------|------------------------------|-------|--------|
| 1 week  | 1 | 0.6996 (0.6931) | +0.008 (0.8)  | +0.10 pts  | 51.1% | no |
| 1 month | 1 | 0.7069 (0.6930) | +0.007 (0.6)  | +0.15 pts  | 50.8% | no |
| 3 months| 1 | 0.7047 (0.6922) | +0.018 (1.2)  | +1.40 pts  | 51.5% | no |
| 1 week  | 2 | 0.6943 (0.6931) | +0.009 (1.1)  | −0.01 pts  | 50.4% | no |
| 1 month | 2 | 0.6977 (0.6930) | +0.011 (0.5)  | +0.12 pts  | 50.0% | no |
| 3 months| 2 | 0.7066 (0.6922) | +0.008 (−0.3) | +0.67 pts  | 49.5% | no |

- Attempt 1 found a faint ranking signal but was overconfident.
- Attempt 2 (`ATTEMPT = 2` in `signal_train.py`) holds out the last year of
  each training window to pick the number of trees and to shrink the odds
  toward 50/50 (Platt scaling). That year usually chose a single tree: what
  worked one year didn't carry into the next.
- For comparison, plain 12-month momentum scored +0.014, +0.001 and −0.017
  over the same years.

The takeaway is that price behaviour alone carries little reliable edge for
S&P 500 stocks in 2020–2026. Rather than keep tuning against the same test
years, which would only fit noise, the next attempt needs new information:
earnings surprises, analyst revisions, fundamentals and news. Both reports
are in `~/.faam/juno/eval-signal-attempt1.json` and `…-attempt2.json`.

FAAM keeps JUNO forecasts switched off until a horizon passes
(`juno_status()["forecastReady"]`), and labels JUNO "In heavy testing"
everywhere.

## The live scorecard

The backtest can only be run so many times before it's tuned to its own test.
The live scorecard can't be, because the future hasn't happened yet:

- Every trading day after the close, FAAM's `scorecard_loop` writes down JUNO
  Signal's odds for 40 well-known stocks (`SCORECARD_SYMBOLS`), at all three
  horizons, as of the last **finished** session.
- When a call comes due, FAAM checks whether the stock really beat SPY.
- Calls are never edited or dropped. They live in `~/.faam/juno_scorecard.json`.
- The list is fixed and never anyone's watchlist, so the scorecard reveals
  nothing about any account.
- Open it from Settings → Forecast model → "See JUNO's live scorecard", from
  the JUNO panel, or tell the voice agent "open JUNO's scorecard".
  `GET /api/juno/scorecard` returns the same data.
- Hit rates show "Too early" until 40 calls are scored. Set
  `FAAM_SCORECARD=0` to switch the loop off.

### Rebuild JUNO Signal

```bash
/usr/bin/python3 juno/signal_data.py                          # ~3 minutes: S&P 500 list + prices
~/.faam/juno/venv/bin/pip install scikit-learn
~/.faam/juno/venv/bin/python juno/signal_train.py             # ~4 minutes: walk-forward test + final model
~/.faam/juno/venv/bin/python -m unittest juno/test_signal.py  # exported trees score exactly like sklearn
```

## Why Llama 3.1 8B and not Llama 4

Meta's newest open models are Llama 4 Scout (109B parameters) and Maverick (400B).
Even compressed to 4-bit, Scout is about 61 GB and won't fit in this Mac's 24 GB.
Llama 3.1 8B is the most capable Llama that runs and trains here. Training a
bigger JUNO would need a cloud GPU; the scripts below work with any MLX model
(`JUNO_BASE_MODEL=…`).

## What JUNO learns

1. **Forecasting.** JUNO reads a stock's recent behaviour (returns over 1 day to
   1 year, volatility, moving averages, RSI, distance from the 52-week high and
   low, recent drawdown, volume) and the market backdrop. It then picks which of
   five bands the stock's return lands in over the next 5, 21 or 63 trading days.
   The bands are sized to each stock's own volatility, so a 3% move means more
   for a utility than for a chip stock. FAAM reads the probability of every band
   from the model, not just its top pick.
   - **No ticker and no date in the prompt.** Llama already knows how famous
     stocks did before 2024, and naming them would let it "remember" instead of
     learn.
2. **Explaining.** FAAM Learn's lessons and quizzes, plus Titan's answers, so
   JUNO explains investing ideas the way FAAM does.

Data is split by date, so the test is honest:

| Split | Windows |
|-------|---------|
| train | ending before 2023-07-01 |
| valid | 2023-07-01 to 2024-06-01 |
| test  | 2024-07-01 onward (after Llama 3.1's knowledge cutoff) |

## Files

Everything heavy lives outside the repo, in `~/.faam/juno/`:

```
~/.faam/juno/
  venv/            Python + Apple MLX (mlx, mlx-lm)
  data/            train/valid/eval .jsonl, meta.json, raw price history
  adapters/        JUNO's trained LoRA weights (adapters.safetensors)
  app/             serve.py + runtime.py, which FAAM starts on demand
  signal/          JUNO Signal's data: universe.json, raw prices, dataset.pkl
  eval-*.json      evaluation reports
```

JUNO Signal's trained model is the exception: it's small, isn't built from
Llama, and ships in the repo as `models/juno-signal.json`.

The Llama weights (4.5 GB) sit in the Hugging Face cache
(`~/.cache/huggingface`).

## Rebuild and retrain

```bash
python3 -m venv ~/.faam/juno/venv && ~/.faam/juno/venv/bin/pip install mlx-lm
/usr/bin/python3 juno/build_data.py          # ~2 minutes: prices + examples
juno/train.sh                                # ~3.5 hours on an M4 (600 LoRA steps)
~/.faam/juno/venv/bin/python juno/evaluate.py          # the fine-tuned JUNO
~/.faam/juno/venv/bin/python juno/evaluate.py --base   # plain Llama, for comparison
juno/install.sh                              # puts the server where FAAM looks
```

### Stopping and resuming

Training saves a checkpoint every 100 steps (`adapters/0000400_adapters.safetensors`,
…). Stop it any time; running `juno/train.sh` again **resumes from the latest
checkpoint** and continues the run instead of restarting it:

- the learning-rate schedule picks up at that step (warmup and cosine, in `lora.yaml`)
- the shuffled data order continues where it stopped, so nothing is trained on twice
- new checkpoints keep the real step numbers (500, 600), never overwriting old ones
- Adam's running averages are saved beside each checkpoint (`…_optimizer.safetensors`)
  and restored on resume

It refuses to resume if `lora.yaml` changed in a way that matters (layers, batch
size, learning rate, schedule…). Use `juno/train.sh --fresh` to start over.
Checkpoints from before the resume support (100–400 in the first run) have no
saved averages, so a resume from one of those restarts Adam's averages; the
step count, schedule and data order still carry on. Tests:
`~/.faam/juno/venv/bin/python -m unittest juno/test_train.py`.

Then pick **Llama X JUNO** under Settings → Forecast model, or tell the voice
agent "switch the forecast model to JUNO". The first forecast takes about 20
seconds while JUNO loads; after that it's a few seconds each.

## Honest limits

- **It's a probability, not a promise.** Short-term stock moves are mostly noise.
  `evaluate.py` compares JUNO with simple baselines ("always say up", "last
  month's direction continues"). Read that report before trusting it.
- **The universe is today's large caps and ETFs.** These are companies that
  survived and grew, which flatters any backtest (survivorship bias).
- **Training uses dividend-adjusted closes; live forecasts use regular closes.**
  The difference is small for these signals.
- **JUNO only runs on a Mac with Apple silicon.** On a Linux server (such as
  Render), FAAM shows it as "coming soon". Hosting JUNO elsewhere means
  pointing `FAAM_JUNO_URL` at a GPU box running `serve.py`'s API.
- **Information, not financial advice.**
