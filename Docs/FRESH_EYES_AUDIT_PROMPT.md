# FRESH EYES — PASTE THIS INTO A NEW AI SESSION

Copy everything between the `=====` lines and paste into a brand-new AI
chat (Cline / ChatGPT / whatever) with access to this repo.

---

```
======================================================================
TASK:  Independent audit of my prop-firm trading bot on REAL data.
======================================================================

You have never seen this codebase. You have no loyalty to any prior
result. Your only job is to TEST my bot using the REAL 3-month MT5
data from my live prop-firm broker, and tell me honestly whether the
numbers are real or fake.

---- THE PROP FIRM (what we're trading for) ----
  The 5%ers "High Stakes" 100K — 2-step challenge.
    * Max Loss        : 10 % STATIC vs $100,000 (never below $90,000)
    * Daily Loss      :  5 % vs day-start equity
    * Step 1 target   : +8 %    ($108,000)
    * Step 2 target   : +5 %
    * News-trading    : NOT allowed
    * Scalping        : allowed (min 60-second hold enforced by bot)
    * Platform        : MT5, server FivePercentOnline-Real

  I deliberately set the bot's internal stop at 4 % total drawdown,
  stricter than the firm's 10 % — extra safety buffer for slippage,
  data drift, weekend gaps. Don't "optimise" this to be looser.

---- THE DATA (real, not synthetic) ----
  data/historical/_provenance.json proves these are real bars pulled
  from my live 5ers MT5 account on 2026-04-23. Each file is ~88 000
  M1 bars covering 2026-01-20 → 2026-04-21. Use these 4 files only:

    data/historical/DE40_M1.csv    (DAX40)
    data/historical/US30_M1.csv
    data/historical/US500_M1.csv   (SP500)
    data/historical/XAUUSD_M1.csv

  News events (Tier-1 macro only):  data/news/tier1_2026.csv

---- THE BOT (current version) ----
  It's a 4-symbol Opening-Range-Breakout (ORB) with a Merton
  dynamic sizer and safety rails. Don't get distracted by old
  V-numbers in the repo — the CURRENT live bot is defined by
  these four files only:

     src/live/v23_live.py              — live engine
     Scripts/backtest_v23_final.py     — the backtest that mirrors it
     src/dynamic_sizer_v21.py          — sizer
     src/dd_breaker.py                 — 4 % DD circuit breaker
     src/momentum/orb.py               — signal math

  Everything else in the repo (v10, v15, v18, v19, v20, etc.) is
  HISTORICAL and NOT the current bot. Ignore it.

---- YOUR TASK — 3 steps, be brutal ----

  1. READ the 5 files above. Understand what the live bot actually
     does. Ignore every other V-numbered file.

  2. RUN the backtest on the real data:

        python Scripts/backtest_v23_final.py

     (If it needs flags, inspect its argparse and pass the defaults.
     Do not touch the sizer parameters, do not change the DD cap.)

  3. REPORT back with ONE page containing only this:

        A) NUMBERS you got on the real 5ers data:
             - total trades, PnL $, return %, max DD %, worst day %
             - same per symbol
             - was the 4 % DD breaker triggered?   YES/NO
             - any trade held < 60 seconds?        YES/NO
             - share of trades opened+closed on the SAME M1 bar (%)

        B) TOP 3 RISKS you see that could make the live bot behave
           differently to the backtest.  Rank them worst first.

        C) VERDICT — exactly one of:
             (i)   Run it on demo for 2 weeks, then go live.
             (ii)  Fix these specific things first: [list].
             (iii) Do NOT deploy — the numbers are not real.

  NO narrative. NO version archaeology. NO praise. Just the audit.

======================================================================
END — over to you.
======================================================================
```
