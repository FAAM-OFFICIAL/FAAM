# Competitive Analysis — Robinhood

> Reference notes on why Robinhood resonated with users. Robinhood is the
> benchmark competitor for FAAM. This captures what made it so good so we can
> decide what to learn from, match, or deliberately do differently.

---

## Core reasons people like Robinhood

- **Commission-free trades** for stocks, ETFs, and options — very attractive if
  you're starting with smaller amounts or trade often.
- **Clean, intuitive mobile and web app** that feels more like a modern consumer
  app than a legacy brokerage platform.
- **Low account minimums** (basically none), so you can get started with very
  little money.

## Features that stand out

- **Fractional shares** — buy small dollar amounts of expensive stocks instead
  of needing the full share price.
- **Stocks, ETFs, options, and crypto in one place**, including 24/7 crypto
  trading and extended-hours / 24-hour stock trading on many tickers.
- **Instant deposits** (with even bigger limits on Robinhood Gold), so you can
  trade right after moving money in instead of waiting days.

## Robinhood Gold perks

- **Subscription (~$5/month)** that unlocks higher instant-deposit limits,
  Morningstar research, and interest on uninvested cash (high-yield cash
  program).
- **Margin access** with the first chunk of margin borrowing interest-free, plus
  some IRA perks like an extra match on eligible contributions.

## Newer AI, social, and pro tools

- **"Legend" and other advanced trading tools** for more serious traders —
  better charting and options tooling compared to the basic app.
- **AI-powered "Custom Scans"** that use an LLM to help screen stocks by criteria
  without writing code, with more AI features planned from their Cortex division.
- **Upcoming "Robinhood Social"** so users can share ideas, follow traders, and
  track what big players (politicians, hedge funds, insiders) are doing.

## Why it clicked with younger / first-time investors

- **Made zero-commission trading the norm** across the industry, which massively
  lowered the barrier to entry.
- **Gamified feel** (smooth UI, notifications, simple flows) plus easy account
  opening, which pulled in a huge wave of first-time investors.
- **Built-in education content** and "learn as you go" resources inside the app
  aimed at beginners.

---

## What FAAM can take from this

A quick map from the strengths above to where FAAM stands today and where it
could go next.

| Robinhood strength | Status in FAAM | Notes |
| --- | --- | --- |
| Clean, modern consumer UI | ✅ Shipped | Professional light/dark dashboard |
| Stocks, ETFs, crypto in one place | ✅ Shipped | Watchlist supports any Yahoo symbol; **asset-class badges** (EQ/ETF/CRYPTO/IX) added |
| AI "Custom Scans" (screen by criteria, no code) | ✅ Shipped | **AI Screener** — describe a scan in plain English → ranked tickers over a live universe |
| "Learn as you go" education | ✅ Shipped | **Learn** — AI tutor dialog with preset topics + free-form questions |
| Fractional shares | ✅ Shipped | Add a position by **$ amount** → fractional shares at the live price |
| Legend-style advanced charting | ✅ Shipped (v1) | **SMA 20 / 50** moving-average overlays; more indicators TBD |
| Social — follow traders, track big players | ⏳ Needs data feed | Insider / 13F / politician-trade data isn't available from the free Yahoo endpoint. Not fabricating it; the screener's "Today's movers" preset covers real activity for now. |

**Takeaway:** Robinhood won by making investing *low-friction, approachable, and
modern*, then layered AI/social/pro tooling on top. FAAM now matches the
approachable + AI-native pieces; the remaining gap (real social/insider feeds)
is a data-licensing problem, not a UI one.

> Note: FAAM is an information and analysis tool, not a brokerage. It does not
> execute trades or move money. Several Robinhood strengths above (commission-free
> execution, margin, instant deposits) are brokerage features and are out of
> scope for FAAM by design.
