# Research log

One entry per test: the rule (fixed before running on real data), the data,
the result, and the verdict. All tests are read-only simulations with
$2,500 max notional and 1 bp/side costs unless noted. The live bot stays on
its current rules until a test passes the playbook bar (README).

## 1. Opening-range breakout trend, single holdout (2026-09-23): FAILED
- **Rule:** 30-min opening range breakout + VWAP side, long or long/short,
  trailing stop 0.3-1.5%, 1 or 3 trades/day (18 variants).
- **Data:** 168 days to 2026-09-22; first 126 in-sample, last 42 held out.
- **Result:** pick = long/short, 0.8% trail, 1/day. In-sample +$209 (124
  trades, mostly March); holdout **-$77** (0/3 months up). Live rules on the
  same holdout: +$27.
- **Verdict:** in-sample gain was luck from picking the best of 18. Holdout spent.

## 2. Breakout + trend-day filters, walk-forward (2026-09-23): NO EDGE
- **Rule:** as above plus filters (wide opening range, relative volume, gap
  in direction, move from open), 30 variants; each ~1-month block traded
  with the variant that did best on all prior days.
- **Data:** 500 days (2024-09-24..2026-09-22); 416 days out-of-sample.
- **Result:** walk-forward pick -$145; per-filter picks -$121..+$23; live
  rules -$119. Synthetic no-trend null test spans about -$450..+$300, so all
  are inside noise. Picks earned +$22..+$168 in training every block but lost
  in 12 of 20 test blocks.
- **Verdict:** no edge; simple 10-minute trend rules on SPY/QQQ don't beat costs.

## 3. First half-hour predicts last half-hour (Gao et al. 2018) (2026-09-23): NO EDGE
- **Rule (no parameters):** sign of prior close -> 10:00 return sets a
  close-30 -> close trade.
- **Data:** SPY and QQQ, 30-min adjusted bars, 2016-01..2026-09-22.
- **Result:** SPY 2,686 trades, **-2.2 bp/trade after costs** (t = -3.7),
  -$1,484 total; about 0 before costs; negative in 9 of 11 years. QQQ -1.8 bp.
- **Verdict:** the published effect has faded (consistent with a 2026
  external retest on 2022-2026 SPX); costs make it a loser.

## 4. Noise-area momentum (Zarattini, Aziz & Barbon 2024): PRIMARY FAILED, QQQ LEAD
- **Rule (paper settings, not tuned):** see `noise.py` docstring. 14-day
  time-of-day bands around max/min(open, prev close); :00/:30 checks; exit on
  max(band, VWAP) trailing stop or the close; vol-targeted size capped at $2,500.
- **Primary test:** SPY 2024-05 onward (after the paper's sample).
- **Result:** primary SPY 2024-05..2026-09-22: 507 trades, +$57 before costs,
  **-$83 after** (t = -0.73). SPY 2016..2024-04 (overlaps paper): +$360 after
  costs, t = +1.49, positive 2018 and 2020-24; 2025 -$37, 2026 -$101. With the
  live bot's close-30 flatten: -$52. QQQ (secondary): 2024-05+ +$136 (t = +1.18),
  2016-2026 +$756 (t = +2.84, Sharpe 0.87).
- **Verdict:** fails its pre-registered primary test; the SPY effect is weak
  and has faded since publication. QQQ is the only lead in any test so far,
  but it was a secondary result, so it needs a fresh forward test (paper,
  shadow first) before it counts.
