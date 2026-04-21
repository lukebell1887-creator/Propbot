# JULY 2026 QUARTERLY RETUNE — CHECKLIST

**Do this on / around 22 July 2026. Takes ~2-3 hours. Fully automated except for the sanity check.**

---

## Why this exists

Markets drift. Volatility regimes shift. Your bot has 3 layers of online adaptation
(quantile gates, Kelly buckets, DD shrinkage) which handle *day-to-day* sentiment,
but the **deep parameters** (z_min_abs, hurst_max_abs, ATR stops, TP mults) are
fixed between retunes. Every 3 months you retune on fresh data so the bot
doesn't quietly decay.

Skip this for 12+ months and the strategy is effectively dead.

---

## The 5-step workflow

```powershell
# ======== Step 1 — Download the latest 3 months of 5%ers bars ========
cd C:\PropBot
git pull
.venv\Scripts\Activate.ps1
python Scripts\download_5ers_3month.py
#    writes data\5ers_3month\<symbol>_M1.csv — fresh Jan-Apr -> Apr-Jul window

# ======== Step 2 — Re-run the v15 optimizer on new data ==============
python Scripts\v15_ultimate_optimizer.py
#    writes Results\v15_ultimate_tuning.json  (per-symbol params)
#    takes ~1-2 hours on a VPS, 30-45 min on a fast laptop

# ======== Step 3 — Walk-forward validate on the untouched next month ==
python Scripts\backtest_v18.py --oos
#    reads fresh params + backtests on the NEXT 1 month never shown to optimizer
#    writes Results\v18_quarterly_retune_YYYYMM.json

# ======== Step 4 — Sanity check (this is the human step) =============
#    Open the JSON in Results\ and verify:
#       net_pnl       > $0       (edge still exists)
#       pf            > 1.5      (profit factor healthy)
#       max_dd_pct    < 5.0      (within 5%ers hard limit)
#       win_rate      > 0.55     (not a lucky fluke)
#       trades        > 30       (enough sample for confidence)
#
#    If ANY of those fail → DO NOT DEPLOY. Keep current params, investigate.
#    If all pass → go to step 5.

# ======== Step 5 — Deploy ============================================
git add Results\v15_ultimate_tuning.json Results\v18_quarterly_retune_*.json
git commit -m "quarterly retune 2026-Q3 (Apr-Jul data)"
git push
#    On the VPS:
.\STOP_BOT.ps1
git pull
.\GO_LIVE.ps1
#    Bot restarts with fresh params + warm Kelly from latest trades.
```

---

## Red-flag thresholds that mean "retune NOW, don't wait"

Check `Results\v18_live_trades.jsonl` or run `.\STATUS.ps1` weekly.
If ANY of these trigger between quarterly retunes, STOP_BOT and retune early:

- Rolling 30-trade live WR drops below **55 %** for 2 weeks straight
- Equity drawdown exceeds **4 %** at any point (daily kill triggers)
- Average realised R per trade < **+0.10** over 50+ trades
- Any single symbol's bucket WR falls below **40 %** (kelly auto-kills it,
  but that's a signal the regime for that symbol has changed)

---

## Next scheduled retunes after July 2026

- **October 2026**  (Q4 2026)
- **January 2027**  (Q1 2027)
- **April 2027**    (Q2 2027)
- ... forever

Put these in your phone calendar NOW. Non-negotiable.

---

## If you forget and 6+ months pass

Don't just retune and go — do the **full safety dance**:

1. Run `backtest_v18.py` on the fresh 3 months WITHOUT deploying
2. If PF < 1.5, the strategy is broken — don't deploy, contact your quant (me)
3. If PF > 1.5 but DD > 5%, redeploy at HALF risk for first 30 trades
4. Only return to full risk after 50 live trades at the new params with PF > 1.5
