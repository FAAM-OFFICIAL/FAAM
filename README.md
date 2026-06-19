# FAAM — Financial AI Agent Manager

A single-page financial dashboard powered by **OpenAI GPT** (default `gpt-4.1-mini`).
Stock data from Yahoo Finance, AI insights and chat from the OpenAI API.

## What's inside

- **Customizable watchlist** — add/remove any ticker right from the rail; saved to `~/.faam/watchlist.json`
- **Portfolio tracker** — add positions (ticker + shares + avg cost); see live market value, unrealized P&L, and totals
- **Main chart** — selectable range (1D / 1W / 1M / 3M / 1Y / 5Y) with live sparklines on each watchlist card
- **AI Insights panel** — GPT generates a tight take on the selected stock
- **Ask FAAM** — chat input at the bottom; opens a dialog for follow-up
- **Light / dark mode** — toggle in the top bar, remembered across sessions

## Run it

Requires Python 3.9+. **No `pip install` needed** — pure stdlib.

```bash
export OPENAI_API_KEY=sk-...          # your OpenAI key
python3 app.py
```

Then open <http://localhost:8765>.

Optional env:
- `FAAM_PORT` — default `8765`
- `FAAM_MODEL` — default `gpt-4.1-mini`. Try `gpt-4.1` (more capable), `gpt-4o`, `gpt-4o-mini`, or any model your key can access.

## Customize the watchlist

Use the **+ Add** card on the rail (or the **Add to watchlist** button) — any
Yahoo Finance symbol works (stocks, ETFs, crypto like `BTC-USD`, indices like
`^GSPC`). Hover a card and click **×** to remove. Your list is saved to
`~/.faam/watchlist.json`. The starting set lives in `DEFAULT_TICKERS` in `app.py`.

## Your data

FAAM stores everything locally under `~/.faam/`:

- `key` — your OpenAI API key (mode 600)
- `watchlist.json` — your tickers
- `portfolio.json` — your positions

Delete any of these to reset that piece.

## Architecture

```
app.py                       stdlib HTTP server
├─ /                         landing page
├─ /dashboard                the app
├─ /download                 builds + streams FAAM.app as a zip
├─ /api/health               {ok, model, provider, ai_enabled}
├─ /api/watchlist            stored tickers + quote/sparkline data
├─ /api/watchlist/add        POST {symbol}  (validates, persists)
├─ /api/watchlist/remove     POST {symbol}
├─ /api/portfolio            positions enriched with live value + P&L
├─ /api/portfolio/add        POST {symbol, shares, cost}
├─ /api/portfolio/remove     POST {id}
├─ /api/stock/<sym>          full quote + history (?range=1mo&interval=1d)
├─ /api/analyze              POST {symbol} — GPT take on the stock
└─ /api/chat                 POST {messages, symbol} — GPT chat with context

static/
├─ index.html                landing page
├─ dashboard.html            dashboard layout
├─ landing.css / styles.css  professional light + dark themes
└─ app.js                    watchlist, portfolio, chart, chat, theme
```

The API key never touches the browser — all OpenAI calls go through the Python
backend.

## Not financial advice

This is a demo. Don't trade real money based on its output without doing your
own research.
