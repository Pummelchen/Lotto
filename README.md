# Lotto & Eurojackpot Analyzer

Two terminal-only, standard-library-only lottery analyzers sharing the same SQLite and statistical helpers.

- **LOTTO 6aus49** — `lotto.py` (implementation in `app.py`)
- **Eurojackpot 5/50 + 2/12** — `eurojackpot.py`
- Shared chi-square/binomial helpers — `stats_common.py`

No third-party packages are required. Python 3.11+.

---

## LOTTO 6aus49

Run:

```bash
python3 lotto.py report
```

`app.py` remains a fully compatible entry point; `lotto.py` is the explicit Lotto-named wrapper.

On first start the Lotto analyzer creates `lotto.db`, loads `data/lotto6aus49.json`, and seeds the aggregate frequency snapshot. The archive has 6,788 raw rows; it keeps the 5,060 comparable modern draws by excluding legacy Wednesday B-draw rows before 2000-12-02.

On every run it refreshes number frequencies from the public LOTTO.de statistics endpoint behind <https://www.lotto.de/lotto-6aus49/statistik/ziehungshaeufigkeit> and stores them in SQLite. A failed or smaller response is rejected with a warning and the stored snapshot is kept.

```bash
# Core report and ticket analysis
python3 lotto.py report
python3 lotto.py analyze 10 20 30 35 40 49
python3 lotto.py assess 10 20 30 35 40 49

# Official prize classes and expected-value estimate
python3 lotto.py prizes
python3 lotto.py ev --jackpot 10000000 --stake 1.20 \
  --other-prizes 1000000,50000,10000,5000,1000,100,10,5

# Random line or 100 ranked lines
python3 lotto.py random
python3 lotto.py best
python3 lotto.py best --runs 25
python3 lotto.py best --runs 1000 --top 5 --profile anti-sharing
python3 lotto.py best --runs 1000 --top 5 --profile empirical  # needs import-players

# Fairness, drift, calibration, patterns, pairs/triples
python3 lotto.py status
python3 lotto.py history
python3 lotto.py drift --years 10
python3 lotto.py drift --window-draws 200 --window-step 100
python3 lotto.py backtest --samples 5000
python3 lotto.py patterns
python3 lotto.py pairs

# Data and exports
python3 lotto.py import-draws draws.csv
python3 lotto.py import-draws draws.csv --header yes  # force/disable numeric header detection
python3 lotto.py import-players player_lines.csv
python3 lotto.py players
python3 lotto.py export results.json
python3 lotto.py export results.csv --format csv

# Refresh, offline mode, snapshot maintenance
python3 lotto.py refresh
python3 lotto.py --offline report
python3 lotto.py reseed
python3 lotto.py save 34 37 41 43 46 49 --label "example"
python3 lotto.py saved
```

Lotto covers number-frequency fairness, adjusted chi-square, observed/theoretical pattern distributions, exact binomial pair/triple tests, player-selection analysis, sharing-risk flags, 0–100 `typical` and `anti-sharing` pattern scores for `best`, official prize-class/expected-value tables, year/rolling-window drift scans, score-calibration backtesting, and a data-status view.

---

## Eurojackpot 5/50 + 2/12

Run:

```bash
python3 eurojackpot.py report
```

Eurojackpot uses a separate database, `eurojackpot.db`, a bundled official row-level archive `data/eurojackpot_draws.csv` (1,003 draws through 2026-10-30), and aggregate fallbacks `data/eurojackpot_full.json` / `data/eurojackpot_current.json`.

On every run it fetches the number-frequency statistics from the LOTTO Hessen endpoint behind <https://www.lotto-hessen.de/eurojackpot/gewinnzahlen-quoten/statistiken>:

`https://www.lotto-hessen.de/getEurojackpotStatisticsCounts`

It is queried once without a date filter for the full 5/50 main-number history and once with `startdate=2022-03-25` for the current 2/12 Euro-number era. Both responses are validated before storage; failed or smaller responses are rejected with a warning and the stored snapshot is kept.

The analyzer uses:
- the **full 5/50 main-number history** from the LOTTO Hessen endpoint, with **WestLotto's statistics page** as a main-number fallback: <https://www.westlotto.de/eurojackpot/statistiken/wieoftgezogen.html>;
- the **current 2/12 Euro-number era only** (since 2022-03-25) from the LOTTO Hessen endpoint. Euro numbers 9–10 were added in 2014 and 11–12 in 2022, so full-history Euro counts are not comparable and are not used for fairness.

```bash
# Core report and ticket analysis (five main + two Euro)
python3 eurojackpot.py report
python3 eurojackpot.py analyze 5 12 23 34 45 3 9

# Official prize classes and expected-value estimate
python3 eurojackpot.py prizes
python3 eurojackpot.py ev --jackpot 10000000 --stake 2.00 \
  --other-prizes 1000000,100000,10000,5000,1000,1000,500,500,100,100,50

# Random line or 100 ranked lines
python3 eurojackpot.py random
python3 eurojackpot.py best
python3 eurojackpot.py best --runs 25
python3 eurojackpot.py best --runs 1000 --top 5 --profile anti-sharing
python3 eurojackpot.py best --runs 1000 --top 5 --profile empirical  # needs import-players

# Number fairness, drift, and calibration
python3 eurojackpot.py status
python3 eurojackpot.py history
python3 eurojackpot.py drift --years 10
python3 eurojackpot.py drift --window-draws 200 --window-step 100
python3 eurojackpot.py backtest --samples 5000
python3 eurojackpot.py patterns

# Optional row-level archive import and observed analyses
python3 eurojackpot.py import-draws eurojackpot.csv --source "my archive"
python3 eurojackpot.py sync-draws             # fetch only new official draws
python3 eurojackpot.py draws
python3 eurojackpot.py import-players players.csv --source "my player sample"
python3 eurojackpot.py players
python3 eurojackpot.py patterns
python3 eurojackpot.py pairs

# Export and local saved tickets
python3 eurojackpot.py export results.json
python3 eurojackpot.py export results.csv --format csv
python3 eurojackpot.py save 5 12 23 34 45 3 9 --label "example"
python3 eurojackpot.py saved

# Refresh, offline mode, bundled snapshot
python3 eurojackpot.py refresh
python3 eurojackpot.py --offline report
python3 eurojackpot.py reseed
```

Eurojackpot covers:

- Official prize classes, exact odds, published payout shares, and optional expected-value estimates for supplied prize amounts.
- Main-number fairness over the full 5/50 history and Euro-number fairness over the current 2/12 era, with adjusted chi-square, p-values, z-scores, and Bonferroni-adjusted extremes.
- Full ticket odds: `C(50,5) * C(12,2) = 139,838,160`.
- Theoretical main-number distributions for sum, odd/even, low/high, birthday range, adjacency, and range, plus Euro sum/parity.
- Ticket analysis, sharing-risk flags, and 0–100 main-number `typical` and `anti-sharing` pattern scores for `best`.
- Main pair/triple family sizes and expected counts.
- Optional row-level draw import (CSV/JSON with date + five main + two Euro numbers) enables observed pattern distributions and exact-binomial pair/triple analysis.
- Bundled official row-level history from LOTTO Hessen, so observed patterns, pairs, drift, and backtesting work immediately. `sync-draws` adds only new draws.
- Player-selection import and measured popularity analysis (`import-players`, `players`).
- Automatic LOTTO Hessen vs WestLotto main-count cross-check, plus `status` for snapshot age, source, cross-check, and imported-row coverage.
- Year-by-year and rolling-window fairness drift analysis, and score-calibration backtesting against historical winning lines.
- JSON/CSV export and locally saved Eurojackpot tickets.

The bundled `data/eurojackpot_draws.csv` contains official LOTTO Hessen historic rows. Use `sync-draws` for new draws, or `import-draws` for your own archive. Historical frequency is explicitly descriptive, not predictive. Third-party row-level data you import remains your responsibility; check its source, attribution, and license before redistributing it. Player-selection data must be imported separately; no representative player sample is bundled.

---

## Scoring profiles

`best --profile typical` is the optional pattern-typicality profile:

- sum typicality 20%
- parity balance 15%
- low/high balance 15%
- range typicality 10%
- no adjacency 20%
- birthday-range spread 20%

`best --profile anti-sharing` is the **default**. It is an explicit, assumption-based alternative:

- rare parity, including all-even/all-odd 35%
- birthday-range avoidance 25%
- no adjacency 20%
- wide range 10%
- decade spread 10%

Anti-sharing rewards the intuition that unusual patterns may be chosen by fewer players. No representative player-selection dataset is bundled, so the score itself is not a measured sharing probability.

`best --profile empirical` uses an imported player-selection sample instead: it scores lines lower when their numbers are heavily selected and applies a ×0.5 penalty when the exact line appears in the sample. Use `import-players` first. This is measured against your sample, not a national player population.

All profiles are heuristics; none changes the odds of any exact ticket.

## Run both best-ticket scans

```bash
./run_best.sh            # refreshes live sources, then runs both
./run_best.sh --offline  # no network refresh
```

The script runs `best --runs 1000 --top 5` for Lotto and Eurojackpot.

## Unified CLI

```bash
python3 cli.py --game lotto report
python3 cli.py --game eurojackpot --offline status
python3 cli.py --game eurojackpot report
```

The dedicated `lotto.py` and `eurojackpot.py` entry points remain available.

## Data sources

- Lotto 6aus49 archive: public `lotto-aktuell/lotto-daten-feed` JSON snapshot through 2026-10-31.
- Lotto live frequencies: `https://www.lotto.de/api/stats/entities.lotto/statistics/0/0`.
- Eurojackpot live frequencies: `https://www.lotto-hessen.de/getEurojackpotStatisticsCounts` (full main history plus `startdate=2022-03-25` for the current Euro era).
- Eurojackpot main-number fallback: `https://www.westlotto.de/eurojackpot/statistiken/wieoftgezogen.html`.
- Eurojackpot official historic draws: `https://www.lotto-hessen.de/getEurojackpotHistoricNumbers?gckey=…` plus its draw-year/key endpoints; bundled in `data/eurojackpot_draws.csv`.
- Lotto prize classes/odds: `https://www.lotto.de/lotto-6aus49/info/gewinnwahrscheinlichkeit`.
- Eurojackpot prize classes/odds: `https://www.lotto.de/eurojackpot/gewinnwahrscheinlichkeit`.
- Eurojackpot statistics page: `https://www.lotto-hessen.de/eurojackpot/gewinnzahlen-quoten/statistiken`.

## Verify

```bash
python3 -m unittest discover -s tests -v
ruff check .
```
