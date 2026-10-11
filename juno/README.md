# Llama X JUNO

FAAM's own stock model: **Meta's Llama 3.1 8B Instruct, fine-tuned by FAAM** on
ten years of daily stock data and FAAM's own teaching material. It runs on the
Mac (Apple silicon) and powers the **Llama X JUNO** forecast model in FAAM.

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
  eval-*.json      evaluation reports
```

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
