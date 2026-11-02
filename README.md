# Lotto & Eurojackpot Analyzer

## Supported games

- **LOTTO 6aus49** — 6 numbers from 1–49 plus a Superzahl from 0–9.
- **Eurojackpot** — 5 main numbers from 1–50 plus 2 Euro numbers from 1–12.

## Mathematical benefits

- **Exact probabilities.** The project computes the full combination space and exact ticket odds:
  - LOTTO 6aus49: 1 in 13,983,816 for six numbers; 1 in 139,838,160 for 6 + Superzahl.
  - Eurojackpot: 1 in 2,118,760 for five main numbers; 1 in 139,838,160 for 5 + 2.
- **Correct fairness testing.** Number frequencies are tested with a variance-adjusted chi-square statistic that accounts for the dependence created by drawing without replacement.
- **Exact category comparisons.** Sum, odd/even, low/high, birthday-range, adjacency, and range distributions are computed combinatorially, so category frequency is not confused with individual-ticket probability.
- **Exact pair/triple tests.** Historical pair and triple frequencies use exact binomial p-values and family-wise correction, not an unsupported normal approximation.
- **Official prize and expected-value analysis.** Prize classes and odds come from official LOTTO.de tables; expected value and break-even jackpots can be calculated from user-supplied prize amounts.
- **Time-window and drift analysis.** Year-by-year and rolling-window fairness scans include multiple-testing correction and clearly describe whether a deviation is statistically notable.
- **Score calibration.** Backtesting compares the scores of historical winning combinations with random combinations. It demonstrates that the scores do not predict draws.
- **Sharing analysis, not prediction.** Birthday, sequence, adjacency, parity, and empirical player-popularity heuristics can help estimate which lines might be shared more often. Any payout benefit is conditional on winning and depends on representative player-selection data.
- **Data-integrity checks.** Snapshots are validated, stale/smaller responses are rejected, official draw archives are reconciled, and LOTTO Hessen main counts are cross-checked against WestLotto.

## Bottom line

A fair lottery remains uniform: every exact ticket has the same chance, and no analysis can improve that chance. The mathematical benefit of this project is accurate probability, distribution, fairness, expected-value, drift, and sharing analysis—not a prediction system.
