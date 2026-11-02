#!/usr/bin/env python3
"""Terminal-only German LOTTO 6aus49 probability and pattern analyzer (Python 3.11+ + SQLite)."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import re
import sqlite3
import sys
import unicodedata
from collections import Counter
from contextlib import closing
from datetime import UTC, date, datetime
from itertools import combinations, pairwise
from pathlib import Path
from urllib.request import Request, urlopen

from stats_common import (
    binomial_two_sided_p,
    bonferroni,
    chi2_sf,
    normal_two_sided_p,
    weighted_chi_square,
)

ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "lotto.db"
DEFAULT_ARCHIVE = ROOT / "data" / "lotto6aus49.json"
LIVE_STATISTICS_URL = "https://www.lotto.de/api/stats/entities.lotto/statistics/0/0"
TOTAL_COMBINATIONS = math.comb(49, 6)
DRAW_COUNT = 5_060
CONSECUTIVE_SIX = 49 - 6 + 1
BIRTHDAY_MAX = 31
LOW_MAX = 24
PAIR_PROBABILITY = math.comb(6, 2) / math.comb(49, 2)
TRIPLE_PROBABILITY = math.comb(6, 3) / math.comb(49, 3)
PAIR_FAMILY = math.comb(49, 2)
TRIPLE_FAMILY = math.comb(49, 3)
HISTORICAL_FREQUENCIES = (630, 617, 640, 625, 626, 685, 633, 585, 630, 625, 652, 589, 563, 587, 603, 617, 618, 611, 637, 607, 574, 645, 592, 615, 646, 654, 623, 594, 622, 606, 643, 653, 651, 592, 599, 624, 626, 633, 616, 613, 632, 628, 640, 616, 554, 589, 610, 625, 665)

PRIZE_CLASSES = (
    (1, "6 + Superzahl", 139_838_160, "15.0%"),
    (2, "6", 15_537_573, "15.0%"),
    (3, "5 + Superzahl", 542_008, "5.2%"),
    (4, "5", 60_223, "15.5%"),
    (5, "4 + Superzahl", 10_324, "4.3%"),
    (6, "4", 1_147, "10.2%"),
    (7, "3 + Superzahl", 567, "8.7%"),
    (8, "3", 63, "41.1%"),
    (9, "2 + Superzahl", 76, "fixed"),
)
PRIZE_SOURCE = "https://www.lotto.de/lotto-6aus49/info/gewinnwahrscheinlichkeit"
PRIZE_COUNTS = (1, 9, 258, 2_322, 13_545, 121_905, 246_820, 2_221_380, 1_851_150)
PRIZE_FULL_OUTCOMES = TOTAL_COMBINATIONS * 10


def database_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def seed_snapshot(connection: sqlite3.Connection) -> None:
    """Write the supplied frequency snapshot into the snapshot table."""
    if len(HISTORICAL_FREQUENCIES) != 49:
        raise ValueError("The supplied frequency snapshot must contain exactly 49 entries.")
    if sum(HISTORICAL_FREQUENCIES) != DRAW_COUNT * 6:
        raise ValueError("The supplied frequency snapshot does not match DRAW_COUNT.")
    created_at = datetime.now(UTC).isoformat()
    connection.executemany(
        "INSERT INTO frequency_snapshot(number, drawn_count, source_note, draw_count, updated_at) VALUES (?, ?, ?, ?, ?)",
        [
            (number, count, "Supplied LOTTO.de frequency snapshot", DRAW_COUNT, created_at)
            for number, count in enumerate(HISTORICAL_FREQUENCIES, 1)
        ],
    )


def snapshot_is_valid(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT COUNT(*), COUNT(DISTINCT draw_count), MIN(number), MAX(number), COALESCE(SUM(drawn_count), 0), COALESCE(MAX(draw_count), 0) FROM frequency_snapshot"
    ).fetchone()
    count, distinct_draw_counts, minimum, maximum, total, draw_count = row
    return (
        count == 49
        and distinct_draw_counts == 1
        and minimum == 1
        and maximum == 49
        and draw_count > 0
        and total == draw_count * 6
    )


def stored_snapshot_draw_count(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT MAX(draw_count) FROM frequency_snapshot").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def get_metadata(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else None


def set_metadata(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)", (key, value))


def archive_fingerprint() -> str:
    if not DEFAULT_ARCHIVE.is_file():
        return ""
    return hashlib.sha256(DEFAULT_ARCHIVE.read_bytes()).hexdigest()


def strict_int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer.")  # noqa: TRY004
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    raise ValueError(f"{label} must be an integer.")


def store_snapshot(counts: list[int], draw_count: int, source_note: str) -> None:
    if len(counts) != 49 or any(count < 0 for count in counts) or draw_count <= 0:
        raise ValueError("Invalid frequency snapshot.")
    if sum(counts) != draw_count * 6:
        raise ValueError("Frequency snapshot total does not match its draw count.")
    updated_at = datetime.now(UTC).isoformat()
    with closing(database_connection()) as connection, connection:
        connection.execute("DELETE FROM frequency_snapshot")
        connection.executemany(
            "INSERT INTO frequency_snapshot(number, drawn_count, source_note, draw_count, updated_at) VALUES (?, ?, ?, ?, ?)",
            [
                (number, count, source_note, draw_count, updated_at)
                for number, count in enumerate(counts, 1)
            ],
        )


def parse_live_statistics(payload: object) -> tuple[list[int], int, str]:
    """Parse the LOTTO.de statistics API payload into 49 counts and a draw count."""
    if not isinstance(payload, dict):
        raise TypeError("LOTTO.de statistics response is not a JSON object.")
    entries = payload.get("counts")
    if not isinstance(entries, list):
        raise TypeError("LOTTO.de statistics response has no counts list.")
    counts_by_number: dict[int, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise TypeError("LOTTO.de statistics count entry is not an object.")
        try:
            number = strict_int(entry["drawNumber"], "drawNumber")
            count = strict_int(entry["count"], "count")
        except KeyError as error:
            raise ValueError("LOTTO.de statistics entry is missing drawNumber/count.") from error
        if not 1 <= number <= 49 or count < 0:
            raise ValueError("LOTTO.de statistics contain an out-of-range number or count.")
        if number in counts_by_number:
            raise ValueError("LOTTO.de statistics contain a duplicate number.")
        counts_by_number[number] = count
    if sorted(counts_by_number) != list(range(1, 50)):
        raise ValueError("LOTTO.de statistics do not contain all 49 numbers.")
    total = sum(counts_by_number.values())
    if total <= 0:
        raise ValueError("LOTTO.de statistics contain no draws.")
    if total % 6 != 0:
        raise ValueError("LOTTO.de statistics total is not divisible by six.")
    counts = [counts_by_number[number] for number in range(1, 50)]
    return counts, total // 6, "LOTTO.de live statistics"


def fetch_live_snapshot(timeout: float = 8.0) -> tuple[list[int], int, str]:
    request = Request(
        LIVE_STATISTICS_URL,
        headers={
            "User-Agent": "LottoAnalyzer/1.0 (educational; Python stdlib)",
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return parse_live_statistics(payload)


def minimum_snapshot_draw_count() -> int:
    with closing(database_connection()) as connection:
        stored = stored_snapshot_draw_count(connection)
    return max(DRAW_COUNT, stored)


def refresh_snapshot() -> None:
    counts, draw_count, source_note = fetch_live_snapshot()
    minimum = minimum_snapshot_draw_count()
    if draw_count < minimum:
        raise ValueError(
            f"LOTTO.de returned {draw_count:,} draws, fewer than the stored/bundled {minimum:,}; keeping the stored snapshot."
        )
    store_snapshot(counts, draw_count, source_note)
    print(f"Frequency snapshot refreshed from {source_note}: {draw_count:,} draws, {sum(counts):,} number appearances.")


def initialize_database(seed_draws: bool = True, scrape: bool = True) -> None:
    """Create tables, ensure the bundled data, then refresh the LOTTO.de frequency snapshot."""
    with closing(database_connection()) as connection, connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS frequency_snapshot (
                number INTEGER PRIMARY KEY CHECK(number BETWEEN 1 AND 49),
                drawn_count INTEGER NOT NULL CHECK(drawn_count >= 0),
                source_note TEXT NOT NULL, draw_count INTEGER NOT NULL CHECK(draw_count > 0),
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS saved_ticket (
                id INTEGER PRIMARY KEY, numbers_json TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS draw (
                id INTEGER PRIMARY KEY, numbers_json TEXT NOT NULL,
                draw_date TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS player_ticket (
                id INTEGER PRIMARY KEY, numbers_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
        """)
        if not snapshot_is_valid(connection) or stored_snapshot_draw_count(connection) < DRAW_COUNT:
            connection.execute("DELETE FROM frequency_snapshot")
            seed_snapshot(connection)
        if seed_draws:
            try:
                archive = load_default_archive()
            except (ValueError, TypeError, OSError) as error:
                print(f"Warning: bundled draw archive unavailable ({error}); continuing without it.", file=sys.stderr)
                archive = []
            if archive:
                try:
                    fingerprint = archive_fingerprint()
                except OSError as error:
                    print(f"Warning: could not fingerprint the bundled archive ({error}).", file=sys.stderr)
                    fingerprint = ""
                user_rows = connection.execute(
                    "SELECT COUNT(*) FROM draw WHERE source != 'bundled archive'"
                ).fetchone()[0]
                stored_fingerprint = get_metadata(connection, "bundled_archive_sha256")
                existing_rows = connection.execute("SELECT COUNT(*) FROM draw").fetchone()[0]
                if user_rows == 0 and (
                    existing_rows == 0 or (fingerprint and stored_fingerprint != fingerprint)
                ):
                    connection.execute("DELETE FROM draw")
                    connection.executemany(
                        "INSERT INTO draw(numbers_json, draw_date, source) VALUES (?, ?, ?)",
                        [(json.dumps(numbers), draw_date, "bundled archive") for numbers, draw_date in archive],
                    )
                    if fingerprint:
                        set_metadata(connection, "bundled_archive_sha256", fingerprint)
    if scrape:
        try:
            counts, draw_count, source_note = fetch_live_snapshot()
            minimum = minimum_snapshot_draw_count()
            if draw_count < minimum:
                raise ValueError(
                    f"LOTTO.de returned {draw_count:,} draws, fewer than the stored/bundled {minimum:,}"
                )
        except (OSError, ValueError, TypeError, sqlite3.Error) as error:
            print(f"Warning: LOTTO.de statistics refresh failed ({error}); using the stored snapshot.", file=sys.stderr)
        else:
            try:
                store_snapshot(counts, draw_count, source_note)
            except (ValueError, sqlite3.Error) as error:
                print(f"Warning: could not store LOTTO.de statistics ({error}); using the stored snapshot.", file=sys.stderr)


def reseed_snapshot() -> None:
    """Discard the stored snapshot and reload the bundled frequency data."""
    with closing(database_connection()) as connection, connection:
        connection.execute("DELETE FROM frequency_snapshot")
        seed_snapshot(connection)
    print(f"Frequency snapshot reloaded from {len(HISTORICAL_FREQUENCIES)} numbers over {DRAW_COUNT:,} draws.")


def build_sum_counts() -> list[int]:
    """Count all ticket sums with dynamic programming, not 14m combinations."""
    ways = [[0] * 280 for _ in range(7)]
    ways[0][0] = 1
    for number in range(1, 50):
        for picked in range(6, 0, -1):
            for ticket_sum in range(279, number - 1, -1):
                ways[picked][ticket_sum] += ways[picked - 1][ticket_sum - number]
    return ways[6]


SUM_COUNTS = build_sum_counts()


def ticket_sum_statistics() -> tuple[float, float]:
    """Return the mean and standard deviation of all six-number ticket sums."""
    mean = sum(ticket_sum * count for ticket_sum, count in enumerate(SUM_COUNTS)) / TOTAL_COMBINATIONS
    variance = sum((ticket_sum - mean) ** 2 * count for ticket_sum, count in enumerate(SUM_COUNTS)) / TOTAL_COMBINATIONS
    return mean, math.sqrt(variance)


SUM_MEAN, SUM_SIGMA = ticket_sum_statistics()


def validate_numbers(numbers: list[int]) -> tuple[int, ...]:
    if len(numbers) != 6:
        raise ValueError("Provide exactly six numbers.")
    if any(not 1 <= number <= 49 for number in numbers):
        raise ValueError("Each number must be from 1 through 49.")
    ordered = tuple(sorted(numbers))
    if len(set(ordered)) != 6:
        raise ValueError("Numbers must be unique.")
    return ordered


def frequency_expectation(draw_count: int) -> tuple[float, float]:
    expected = draw_count * 6 / 49
    sigma = math.sqrt(draw_count * (6 / 49) * (43 / 49))
    return expected, sigma


def adjusted_chi_square(counts: list[int], draw_count: int) -> tuple[float, float]:
    """Fairness statistic corrected for the negative dependence within a 6/49 draw."""
    expected, _ = frequency_expectation(draw_count)
    eigenvalue = 6 / 49 - PAIR_PROBABILITY
    statistic = sum((count - expected) ** 2 for count in counts) / (draw_count * eigenvalue)
    return statistic, chi2_sf(statistic, 48)


def pearson_chi_square(counts: list[int], draw_count: int) -> float:
    expected, _ = frequency_expectation(draw_count)
    return sum((count - expected) ** 2 / expected for count in counts)

def odd_even_distribution() -> list[int]:
    return [math.comb(25, odd) * math.comb(24, 6 - odd) for odd in range(7)]


def low_high_distribution() -> list[int]:
    return [math.comb(LOW_MAX, low) * math.comb(49 - LOW_MAX, 6 - low) for low in range(7)]


def birthday_distribution() -> list[int]:
    return [math.comb(BIRTHDAY_MAX, low) * math.comb(49 - BIRTHDAY_MAX, 6 - low) for low in range(7)]


def adjacency_distribution() -> list[int]:
    """Exact count of six-number subsets by number of adjacent pairs."""
    state = [[[0, 0] for _ in range(6)] for _ in range(7)]
    state[0][0][0] = 1
    for _ in range(1, 50):
        next_state = [[[0, 0] for _ in range(6)] for _ in range(7)]
        for chosen in range(7):
            for adjacent in range(6):
                for previous in (0, 1):
                    ways = state[chosen][adjacent][previous]
                    if not ways:
                        continue
                    next_state[chosen][adjacent][0] += ways
                    if chosen < 6:
                        next_state[chosen + 1][adjacent + previous][1] += ways
        state = next_state
    return [state[6][adjacent][0] + state[6][adjacent][1] for adjacent in range(6)]


def range_distribution() -> list[int]:
    counts = [0] * 49
    for span in range(5, 49):
        counts[span] = (49 - span) * math.comb(span - 1, 4)
    return counts


ODD_EVEN_COUNTS = odd_even_distribution()
LOW_HIGH_COUNTS = low_high_distribution()
ADJACENCY_COUNTS = adjacency_distribution()
RANGE_COUNTS = range_distribution()
MAX_SUM_COUNT = max(SUM_COUNTS)
MAX_ODD_EVEN_COUNT = max(ODD_EVEN_COUNTS)
MAX_LOW_HIGH_COUNT = max(LOW_HIGH_COUNTS)
MAX_ADJACENCY_COUNT = max(ADJACENCY_COUNTS)
MAX_RANGE_COUNT = max(RANGE_COUNTS)


def adjacent_count(numbers: tuple[int, ...]) -> int:
    return sum(1 for earlier, later in pairwise(numbers) if later == earlier + 1)


def longest_run(numbers: tuple[int, ...]) -> int:
    longest = current = 1
    for earlier, later in pairwise(numbers):
        current = current + 1 if later == earlier + 1 else 1
        longest = max(longest, current)
    return longest


def analyze_ticket(numbers: tuple[int, ...]) -> dict[str, object]:
    ordered = tuple(sorted(numbers))
    ticket_sum = sum(ordered)
    odd_count = sum(number % 2 for number in ordered)
    return {
        "numbers": ordered,
        "sum": ticket_sum,
        "sum_count": SUM_COUNTS[ticket_sum],
        "odd_count": odd_count,
        "even_count": 6 - odd_count,
        "parity_count": math.comb(25, odd_count) * math.comb(24, 6 - odd_count),
        "longest_run": longest_run(ordered),
        "adjacent_count": adjacent_count(ordered),
        "birthday_count": sum(number <= BIRTHDAY_MAX for number in ordered),
        "all_low": all(number <= BIRTHDAY_MAX for number in ordered),
    }


def assess_ticket(numbers: tuple[int, ...]) -> list[tuple[str, str]]:
    """Return qualitative popularity/sharing flags, never draw-probability claims."""
    ordered = tuple(sorted(numbers))
    birthday_count = sum(number <= BIRTHDAY_MAX for number in ordered)
    adjacency = adjacent_count(ordered)
    run = longest_run(ordered)
    flags: list[tuple[str, str]] = []
    if birthday_count == 6:
        flags.append(("warn", "All six numbers are in the birthday range (1–31)."))
    elif birthday_count >= 5:
        flags.append(("warn", f"{birthday_count} of 6 numbers are in the birthday range (1–31)."))
    if run >= 3:
        flags.append(("warn", f"Contains a consecutive run of {run} numbers."))
    if adjacency >= 3:
        flags.append(("warn", f"Contains {adjacency} adjacent-number pairs."))
    if all(number % 5 == 0 for number in ordered):
        flags.append(("warn", "All numbers are multiples of five."))
    if all(number % 2 == 0 for number in ordered):
        flags.append(("warn", "All six numbers are even."))
    if all(number % 2 == 1 for number in ordered):
        flags.append(("warn", "All six numbers are odd."))
    decades = {number // 10 for number in ordered}
    if len(decades) == 1:
        flags.append(("warn", "All six numbers fall in the same decade."))
    if not flags:
        flags.append(("good", "No obvious birthday-only or simple sequence pattern detected."))
    return flags


def score_ticket(
    numbers: tuple[int, ...], profile: str = "anti-sharing"
) -> tuple[float, dict[str, float]]:
    """Score a ticket 0-100 using a documented heuristic profile.

    ``typical`` rewards common statistical categories. ``anti-sharing`` rewards
    rare parity, birthday-range avoidance, no adjacency, wide range, and decade
    spread. Neither profile is a probability of winning.
    """
    if len(numbers) != 6 or len(set(numbers)) != 6 or any(not 1 <= number <= 49 for number in numbers):
        raise ValueError("A score requires six distinct numbers from 1 through 49.")
    if profile not in {"typical", "anti-sharing"}:
        raise ValueError("Profile must be 'typical' or 'anti-sharing'.")
    ordered = tuple(sorted(numbers))
    analysis = analyze_ticket(ordered)
    sum_value = int(analysis["sum"])
    odd_count = int(analysis["odd_count"])
    low_count = sum(number <= LOW_MAX for number in ordered)
    span = ordered[-1] - ordered[0]
    high_birthday_count = sum(number > BIRTHDAY_MAX for number in ordered)
    if profile == "typical":
        components = {
            "sum": SUM_COUNTS[sum_value] / MAX_SUM_COUNT,
            "parity": ODD_EVEN_COUNTS[odd_count] / MAX_ODD_EVEN_COUNT,
            "low_high": LOW_HIGH_COUNTS[low_count] / MAX_LOW_HIGH_COUNT,
            "range": RANGE_COUNTS[span] / MAX_RANGE_COUNT,
            "adjacency": ADJACENCY_COUNTS[int(analysis["adjacent_count"])] / MAX_ADJACENCY_COUNT,
            "birthday": min(1.0, high_birthday_count / 3),
        }
        score = 100 * (
            0.20 * components["sum"]
            + 0.15 * components["parity"]
            + 0.15 * components["low_high"]
            + 0.10 * components["range"]
            + 0.20 * components["adjacency"]
            + 0.20 * components["birthday"]
        )
    else:
        components = {
            "parity_rarity": 1.0 - ODD_EVEN_COUNTS[odd_count] / MAX_ODD_EVEN_COUNT,
            "birthday": min(1.0, high_birthday_count / 3),
            "adjacency": ADJACENCY_COUNTS[int(analysis["adjacent_count"])] / MAX_ADJACENCY_COUNT,
            "range_spread": min(1.0, max(0.0, (span - 5) / 43)),
            "decade_spread": (len({number // 10 for number in ordered}) - 1) / 4,
        }
        score = 100 * (
            0.35 * components["parity_rarity"]
            + 0.25 * components["birthday"]
            + 0.20 * components["adjacency"]
            + 0.10 * components["range_spread"]
            + 0.10 * components["decade_spread"]
        )
    if len({number // 10 for number in ordered}) == 1:
        score *= 0.90
    if all(number % 5 == 0 for number in ordered):
        score *= 0.95
    return round(max(0.0, min(100.0, score)), 1), {
        key: round(value, 4) for key, value in components.items()
    }


def short_flags(numbers: tuple[int, ...]) -> str:
    flags = assess_ticket(numbers)
    if flags and flags[0][0] == "good":
        return "none"
    labels: list[str] = []
    for _level, message in flags:
        text = message.lower()
        if "birthday" in text:
            label = "birthday"
        elif "consecutive run" in text:
            label = "run"
        elif "adjacent" in text:
            label = "adj"
        elif "multiples" in text:
            label = "mult5"
        elif "even" in text:
            label = "even"
        elif "odd" in text:
            label = "odd"
        elif "decade" in text:
            label = "decade"
        else:
            label = "flag"
        if label not in labels:
            labels.append(label)
    return ",".join(labels) if labels else "none"


def generate_ranked_tickets(
    runs: int = 100,
    rng: random.Random | None = None,
    profile: str = "anti-sharing",
    popularity: dict[str, object] | None = None,
) -> list[tuple[float, tuple[int, ...], dict[str, float]]]:
    if runs <= 0:
        raise ValueError("The number of runs must be a positive integer.")
    generator = rng if rng is not None else random.SystemRandom()
    results: list[tuple[float, tuple[int, ...], dict[str, float]]] = []
    for _ in range(runs):
        numbers = tuple(sorted(generator.sample(range(1, 50), 6)))
        if profile == "empirical":
            if popularity is None:
                raise ValueError("Empirical scoring requires player-selection data.")
            score, components = empirical_sharing_score(numbers, popularity)
        else:
            score, components = score_ticket(numbers, profile=profile)
        results.append((score, numbers, components))
    results.sort(key=lambda item: (-item[0], item[1]))
    return results


def print_ranked_tickets(
    runs: int = 100,
    rng: random.Random | None = None,
    top: int = 0,
    profile: str = "anti-sharing",
    popularity: dict[str, object] | None = None,
) -> None:
    if top < 0:
        raise ValueError("--top must be zero or positive.")
    if profile == "empirical" and popularity is None:
        popularity = player_popularity()
    results = generate_ranked_tickets(runs, rng, profile=profile, popularity=popularity)
    displayed = results[:top] if top else results
    label = "empirical" if profile == "empirical" else ("anti-sharing" if profile == "anti-sharing" else "typical")
    print(f"\nLOTTO 6aus49 — {label} profile — top {len(displayed)} of {runs:,} random tickets\n{'=' * 104}")
    print("Score is a pattern/sharing heuristic, not a draw probability.")
    print("Every exact ticket still has probability 1 in 13,983,816.")
    if profile == "empirical":
        print("Score is based on inverse measured number popularity from the imported player sample.")
        print("The exact-combination duplicate penalty is ×0.5. Measured popularity is sample-specific.")
    elif profile == "anti-sharing":
        print("Weights: parity rarity 35%, birthday spread 25%, no adjacency 20%, range spread 10%, decade spread 10%.")
        print("Anti-sharing is an assumption-based heuristic; no player-selection data is used.")
        print("Pattern flags remain descriptive; rare parity is rewarded by this profile.")
    else:
        print("Weights: sum 20%, parity 15%, low/high 15%, range 10%, adjacency 20%, birthday spread 20%.")
    print("\nRank | Numbers             | Score | Sum | Odd/Even | Low/High | ≤31 | Adj | Run | Flags")
    print("-" * 104)
    for rank, (score, numbers, _components) in enumerate(displayed, 1):
        analysis = analyze_ticket(numbers)
        low_count = sum(number <= LOW_MAX for number in numbers)
        print(
            f"{rank:4} | {' '.join(f'{number:2}' for number in numbers):19} | "
            f"{score:5.1f}% | {int(analysis['sum']):4} | "
            f"{int(analysis['odd_count'])}/{int(analysis['even_count'])}      | "
            f"{low_count}/{6 - low_count}      | {int(analysis['birthday_count']):3} | "
            f"{int(analysis['adjacent_count']):3} | {int(analysis['longest_run']):3} | "
            f"{short_flags(numbers)}"
        )
    scores = [score for score, _numbers, _components in results]
    print(
        f"\nHighest: {max(scores):.1f}% | Lowest: {min(scores):.1f}% | "
        f"Mean: {sum(scores) / len(scores):.1f}% | Displayed mean: "
        f"{sum(score for score, _numbers, _components in displayed) / len(displayed):.1f}%"
    )
    if profile == "empirical":
        print("Less-selected numbers and non-duplicated combinations score higher in this sample.")
    elif profile == "anti-sharing":
        print("Rare parity, numbers above 31, no adjacency, wide range, and decade spread score higher.")
    else:
        print("Balanced 3/3 parity, sums near 150, no adjacency, and numbers above 31 score higher.")
    print("The ranking is not a forecast and cannot improve the chance of winning.")


def format_odds(denominator: float) -> str:
    if math.isclose(denominator, round(denominator), rel_tol=1e-12, abs_tol=1e-9):
        return f"1 in {round(denominator):,}"
    return f"1 in {denominator:,.2f}"


def one_in(category_count: int) -> str:
    return format_odds(TOTAL_COMBINATIONS / category_count)


def any_prize_probability() -> float:
    """Probability that one ticket wins any German LOTTO prize class."""

    def matches(correct: int) -> int:
        return math.comb(6, correct) * math.comb(43, 6 - correct)

    winning_tenths = 10 * sum(matches(correct) for correct in range(3, 7)) + matches(2)
    return winning_tenths / (10 * TOTAL_COMBINATIONS)


def table_any_prize_probability() -> float:
    return sum(PRIZE_COUNTS) / PRIZE_FULL_OUTCOMES


def parse_prize_amounts(text: str, count: int) -> list[float]:
    if not text.strip():
        return [0.0] * count
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != count:
        raise ValueError(f"Provide exactly {count} comma-separated prize amounts.")
    amounts: list[float] = []
    for part in parts:
        try:
            value = float(part)
        except ValueError as error:
            raise ValueError(f"Invalid prize amount: {part!r}") from error
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"Prize amounts must be non-negative finite numbers: {part!r}")
        amounts.append(value)
    return amounts


def calculate_ev(
    jackpot: float = 0.0,
    other_prizes: list[float] | None = None,
    stake: float = 1.0,
) -> dict[str, object]:
    other = [0.0] * (len(PRIZE_CLASSES) - 1) if other_prizes is None else list(other_prizes)
    if len(other) != len(PRIZE_CLASSES) - 1:
        raise ValueError(f"Provide exactly {len(PRIZE_CLASSES) - 1} lower-class prize amounts.")
    if not math.isfinite(jackpot) or jackpot < 0:
        raise ValueError("Jackpot must be a non-negative finite number.")
    if not math.isfinite(stake) or stake <= 0:
        raise ValueError("Stake must be a positive finite number.")
    if any(not math.isfinite(value) or value < 0 for value in other):
        raise ValueError("Prize amounts must be non-negative finite numbers.")
    amounts = [jackpot, *other]
    contributions = [
        amount * count / PRIZE_FULL_OUTCOMES
        for amount, count in zip(amounts, PRIZE_COUNTS)
    ]
    expected = sum(contributions)
    lower_expected = sum(contributions[1:])
    jackpot_odds = PRIZE_CLASSES[0][2]
    return {
        "amounts": amounts,
        "contributions": contributions,
        "expected_value": expected,
        "stake": stake,
        "net": expected - stake,
        "return_pct": expected / stake,
        "break_even_jackpot": max(0.0, (stake - lower_expected) * jackpot_odds),
        "jackpot_only_break_even": stake * jackpot_odds,
    }


def print_prizes() -> None:
    print(f"\nLOTTO 6aus49 official prize classes\n{'=' * 72}")
    print("Class | Match | Official odds | Exact odds | Payout share")
    for (class_no, match, odds, share), count in zip(PRIZE_CLASSES, PRIZE_COUNTS):
        exact = format_odds(PRIZE_FULL_OUTCOMES / count)
        print(f"{class_no:5} | {match:16} | 1 in {odds:>13,} | {exact:>15} | {share}")
    print(f"\nAny prize class: {format_odds(1 / table_any_prize_probability())}")
    print(f"Source: {PRIZE_SOURCE}")
    print("Official odds are published rounded values; exact odds use the official game rules.")
    print("Actual prizes are pari-mutuel except the fixed class 9.")


def print_ev(jackpot: float, other_prizes_text: str, stake: float) -> None:
    other = parse_prize_amounts(other_prizes_text, len(PRIZE_CLASSES) - 1)
    result = calculate_ev(jackpot, other, stake)
    amounts = result["amounts"]  # type: ignore[index]
    contributions = result["contributions"]  # type: ignore[index]
    print(f"\nLOTTO 6aus49 expected-value estimate\n{'=' * 72}")
    print("Class | Match | Exact odds | Prize amount | EV contribution")
    for (class_no, match, _odds, _share), count, amount, contribution in zip(
        PRIZE_CLASSES, PRIZE_COUNTS, amounts, contributions  # type: ignore[arg-type]
    ):
        print(f"{class_no:5} | {match:16} | {format_odds(PRIZE_FULL_OUTCOMES / count):>15} | {amount:12,.2f} | {contribution:12.6f}")
    print(f"\nStake per ticket: {stake:,.2f}")
    print(f"Expected return: {float(result['expected_value']):,.6f} ({float(result['return_pct']):.2%} of stake)")
    print(f"Net expected value: {float(result['net']):+,.6f}")
    print(f"Break-even jackpot with supplied lower prizes: {float(result['break_even_jackpot']):,.2f}")
    print(f"Break-even jackpot if only class 1 paid: {float(result['jackpot_only_break_even']):,.2f}")
    print("Only the prize amounts you supplied are counted. Pari-mutuel amounts and sharing vary.")


def load_snapshot() -> tuple[int, list[sqlite3.Row]]:
    """Load and validate the frequency snapshot; return its draw count and rows."""
    with closing(database_connection()) as connection:
        rows = connection.execute("SELECT number, drawn_count, draw_count, source_note, updated_at FROM frequency_snapshot ORDER BY number").fetchall()
    if [row["number"] for row in rows] != list(range(1, 50)):
        raise ValueError("Frequency snapshot is incomplete; run `python3 app.py reseed`.")
    draw_counts = {row["draw_count"] for row in rows}
    if len(draw_counts) != 1:
        raise ValueError("Frequency snapshot has inconsistent draw counts; run `python3 app.py reseed`.")
    return draw_counts.pop(), rows


def snapshot_updated_at() -> str:
    with closing(database_connection()) as connection:
        row = connection.execute("SELECT updated_at FROM frequency_snapshot LIMIT 1").fetchone()
    return str(row["updated_at"]) if row else ""


def load_draws() -> list[tuple[tuple[int, ...], str]]:
    with closing(database_connection()) as connection:
        rows = connection.execute("SELECT numbers_json, draw_date FROM draw ORDER BY id").fetchall()
    return [(tuple(json.loads(row["numbers_json"])), row["draw_date"]) for row in rows]


def load_player_tickets() -> list[tuple[int, ...]]:
    with closing(database_connection()) as connection:
        rows = connection.execute("SELECT numbers_json FROM player_ticket ORDER BY id").fetchall()
    return [tuple(json.loads(row["numbers_json"])) for row in rows]


def draw_date_range(draws: list[tuple[tuple[int, ...], str]]) -> tuple[str, str]:
    dates = sorted(draw_date for _, draw_date in draws if draw_date)
    return (dates[0], dates[-1]) if dates else ("", "")


def user_imported_draw_count() -> int:
    with closing(database_connection()) as connection:
        row = connection.execute("SELECT COUNT(*) FROM draw WHERE source != 'bundled archive'").fetchone()
    return int(row[0])


def frequency_counts() -> tuple[list[int], int, str]:
    """Use explicit user imports; otherwise use the refreshed aggregate snapshot."""
    if user_imported_draw_count():
        draws = load_draws()
        counts = [0] * 49
        for numbers, _ in draws:
            for number in numbers:
                counts[number - 1] += 1
        return counts, len(draws), "imported draw archive"
    draw_count, rows = load_snapshot()
    source_note = rows[0]["source_note"] if rows else "supplied frequency snapshot"
    return [row["drawn_count"] for row in rows], draw_count, source_note


def observed_patterns(draws: list[tuple[tuple[int, ...], str]]) -> dict[str, object]:
    draw_count = len(draws)
    odd = [0] * 7
    low = [0] * 7
    birthday = [0] * 7
    adjacency = [0] * 6
    longest = [0] * 7
    sums: list[int] = []
    ranges: list[int] = []
    sum_counts = [0] * 280
    range_counts = [0] * 49
    for numbers, _ in draws:
        ordered = tuple(sorted(numbers))
        odd[sum(number % 2 for number in ordered)] += 1
        low[sum(number <= LOW_MAX for number in ordered)] += 1
        birthday[sum(number <= BIRTHDAY_MAX for number in ordered)] += 1
        adjacency[adjacent_count(ordered)] += 1
        longest[longest_run(ordered)] += 1
        ticket_sum = sum(ordered)
        span = ordered[-1] - ordered[0]
        sums.append(ticket_sum)
        ranges.append(span)
        sum_counts[ticket_sum] += 1
        range_counts[span] += 1
    mean_sum = sum(sums) / draw_count
    sd_sum = math.sqrt(sum((value - mean_sum) ** 2 for value in sums) / draw_count)
    return {
        "draws": draw_count,
        "odd": odd,
        "low": low,
        "birthday": birthday,
        "adjacency": adjacency,
        "longest": longest,
        "sum_counts": sum_counts,
        "range_counts": range_counts,
        "sum_mean": mean_sum,
        "sum_sd": sd_sum,
        "sum_median": sorted(sums)[draw_count // 2],
        "range_mean": sum(ranges) / draw_count,
        "adjacency_mean": sum(index * count for index, count in enumerate(adjacency)) / draw_count,
    }


def sum_bin_counts(observed_counts: list[int], draw_count: int, width: int = 5) -> tuple[list[int], list[float]]:
    observed: list[int] = []
    expected: list[float] = []
    for start in range(21, 280, width):
        end = min(start + width, 280)
        observed.append(sum(observed_counts[start:end]))
        expected.append(sum(SUM_COUNTS[start:end]) / TOTAL_COMBINATIONS * draw_count)
    return observed, expected


def range_bin_counts(observed_counts: list[int], draw_count: int, width: int = 5) -> tuple[list[int], list[float]]:
    theoretical = range_distribution()
    observed: list[int] = []
    expected: list[float] = []
    for start in range(5, 49, width):
        end = min(start + width, 49)
        observed.append(sum(observed_counts[start:end]))
        expected.append(sum(theoretical[start:end]) / TOTAL_COMBINATIONS * draw_count)
    return observed, expected


def combination_statistics(draws: list[tuple[tuple[int, ...], str]], size: int) -> dict[str, object]:
    counts: Counter[tuple[int, ...]] = Counter()
    for numbers, _ in draws:
        ordered = tuple(sorted(numbers))
        counts.update(combinations(ordered, size))
    draw_count = len(draws)
    probability = math.comb(6, size) / math.comb(49, size)
    family = math.comb(49, size)
    expected = draw_count * probability
    standard_deviation = math.sqrt(draw_count * probability * (1 - probability))
    items: list[dict[str, object]] = []
    for combination in combinations(range(1, 50), size):
        observed = counts.get(combination, 0)
        z_score = (observed - expected) / standard_deviation
        p_value = binomial_two_sided_p(observed, draw_count, probability)
        items.append({
            "combination": combination,
            "observed": observed,
            "expected": expected,
            "z_score": z_score,
            "p_value": p_value,
            "adjusted_p": bonferroni(p_value, family),
        })
    top = sorted(items, key=lambda item: (-int(item["observed"]), item["combination"]))[:10]
    bottom = sorted(items, key=lambda item: (int(item["observed"]), item["combination"]))[:10]
    never = sum(1 for item in items if item["observed"] == 0)
    return {
        "family": family,
        "probability": probability,
        "expected": expected,
        "standard_deviation": standard_deviation,
        "top": top,
        "bottom": bottom,
        "unique": family - never,
        "never": never,
        "expected_never": family * (1 - probability) ** draw_count,
    }


def player_summary(tickets: list[tuple[int, ...]]) -> dict[str, object]:
    count = len(tickets)
    counts = [0] * 49
    birthday_slots = 0
    combinations_count: Counter[tuple[int, ...]] = Counter()
    for numbers in tickets:
        ordered = tuple(sorted(numbers))
        combinations_count[ordered] += 1
        for number in ordered:
            counts[number - 1] += 1
            if number <= BIRTHDAY_MAX:
                birthday_slots += 1
    slots = count * 6
    top = [
        {"number": number, "count": counts[number - 1]}
        for number in sorted(range(1, 50), key=lambda number: (-counts[number - 1], number))[:8]
    ]
    duplicates = [
        {"numbers": list(combination), "count": occurrences}
        for combination, occurrences in combinations_count.items()
        if occurrences > 1
    ]
    duplicates.sort(key=lambda item: (-int(item["count"]), item["numbers"]))
    return {
        "tickets": count,
        "slots": slots,
        "birthday_slots": birthday_slots,
        "birthday_share": birthday_slots / slots if slots else 0.0,
        "distinct": len(combinations_count),
        "duplicate_entries": count - len(combinations_count),
        "top": top,
        "duplicates": duplicates[:10],
        "expected_slots_per_number": slots / 49 if slots else 0.0,
    }


def normalize_draw_date(value: object) -> str:
    text = str(value).strip()
    if not text:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    match = re.fullmatch(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", text)
    if match:
        return f"{match.group(3)}-{int(match.group(2)):02d}-{int(match.group(1)):02d}"
    return ""


def normalize_header(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value)
    ascii_text = "".join(character for character in decomposed if unicodedata.category(character) != "Mn")
    return re.sub(r"[^a-z0-9]", "", ascii_text.lower())


def parse_number_cell(value: object) -> int | None:
    text = str(value).strip()
    if not re.fullmatch(r"\d{1,2}", text):
        return None
    number = int(text)
    return number if 1 <= number <= 49 else None


def pick_six(values: list[object]) -> tuple[int, ...] | None:
    numbers: list[int] = []
    for value in values:
        number = parse_number_cell(value)
        if number is None:
            continue
        if number in numbers:
            return None
        numbers.append(number)
        if len(numbers) == 6:
            break
    return tuple(sorted(numbers)) if len(numbers) == 6 else None


DATE_PATTERN = re.compile(r"\d{1,4}[-./]\d{1,2}[-./]\d{1,4}")


def is_date_like(value: object) -> bool:
    text = str(value).strip()
    return bool(DATE_PATTERN.search(text))


def find_number_window(cells: list[object]) -> tuple[int, ...] | None:
    date_index = next((index for index, cell in enumerate(cells) if is_date_like(cell)), None)
    starts = [date_index + 1] if date_index is not None else []
    starts.extend(range(max(0, len(cells) - 5)))
    for start in starts:
        if start < 0 or start + 6 > len(cells):
            continue
        numbers = pick_six(cells[start:start + 6])
        if numbers:
            return numbers
    return None


def looks_like_numeric_header(row: list[str], next_row: list[str] | None = None) -> bool:
    """Recognize numeric label rows, including the common sequential form 1,2,3,4,5,6."""
    values: list[int] = []
    for cell in row:
        text = str(cell).strip()
        if not text:
            continue
        if not re.fullmatch(r"\d+", text):
            return False
        values.append(int(text))
    if len(values) < 6:
        return False
    if values == list(range(1, len(values) + 1)) or values == list(range(len(values))):
        return True
    return bool(
        next_row is not None
        and (len(row) != len(next_row) or any(is_date_like(cell) for cell in next_row))
    )


def detect_index_offset(rows: list[list[str]]) -> int:
    """Detect a leading sequential index column in headerless CSV rows."""
    if len(rows) < 2:
        return 0
    first_values: list[int] = []
    for row in rows:
        if not row:
            return 0
        text = str(row[0]).strip()
        if not re.fullmatch(r"\d+", text):
            return 0
        first_values.append(int(text))
    sequential = first_values == list(range(1, len(rows) + 1)) or first_values == list(range(len(rows)))
    if not sequential:
        return 0
    if all(len(row) >= 7 and pick_six(row[1:7]) is not None for row in rows):
        return 1
    return 0


def find_number_column(header: list[str], position: int) -> int | None:
    candidates = {
        f"n{position}", f"number{position}", f"zahl{position}", f"ziffer{position}",
        f"ball{position}", f"gewinnzahl{position}", f"main{position}", f"lottozahl{position}",
    }
    return next((index for index, name in enumerate(header) if name in candidates), None)


def parse_csv_text(text: str, header_mode: str = "auto") -> list[tuple[tuple[int, ...], str]]:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("The file is empty.")
    delimiter = max([",", ";", "\t"], key=lambda candidate: lines[0].count(candidate))
    rows = list(csv.reader(lines, delimiter=delimiter))
    header = [normalize_header(cell) for cell in rows[0]]
    number_indices = [find_number_column(header, position) for position in range(1, 7)]
    if header_mode not in {"auto", "yes", "no"}:
        raise ValueError("--header must be auto, yes, or no.")
    has_number_header = header_mode != "no" and all(index is not None for index in number_indices)
    date_index = next(
        (index for index, name in enumerate(header) if name in {"date", "datum", "drawdate", "ziehungstag", "day"}),
        None,
    )
    numeric_header = (
        header_mode != "no"
        and not has_number_header
        and len(rows) > 1
        and looks_like_numeric_header(rows[0], rows[1])
    )
    if header_mode == "yes" or numeric_header:
        first_row_numbers = None
        start_row = 1
    elif header_mode == "no":
        first_row_numbers = find_number_window(rows[0])
        if date_index is None:
            date_index = next((index for index, cell in enumerate(rows[0]) if is_date_like(cell)), None)
        start_row = 0
    else:
        first_row_numbers = find_number_window(rows[0])
        if not has_number_header and date_index is None:
            date_index = next((index for index, cell in enumerate(rows[0]) if is_date_like(cell)), None)
        start_row = 1 if has_number_header or first_row_numbers is None else 0
    if not has_number_header and date_index is None and start_row < len(rows):
        date_index = next((index for index, cell in enumerate(rows[start_row]) if is_date_like(cell)), None)
    data_rows = rows[start_row:]
    index_offset = 0 if has_number_header else detect_index_offset(data_rows)
    records: list[tuple[tuple[int, ...], str]] = []
    for row in data_rows:
        if has_number_header:
            numbers = pick_six([row[index] if index is not None and index < len(row) else "" for index in number_indices])
        elif index_offset and len(row) >= index_offset + 6:
            numbers = pick_six(row[index_offset:index_offset + 6])
            if numbers is None:
                numbers = find_number_window(row)
        else:
            numbers = find_number_window(row)
        if numbers is None:
            continue
        draw_date = normalize_draw_date(row[date_index]) if date_index is not None and date_index < len(row) else ""
        records.append((numbers, draw_date))
    if not records:
        raise ValueError("No valid six-number rows found in the CSV.")
    return records


def parse_json_text(text: str) -> list[tuple[tuple[int, ...], str]]:
    data = json.loads(text)
    if isinstance(data, dict):
        for key in ("draws", "rows", "results", "data", "tickets"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            single_keys = ("n", "numbers", "zahlen", "main", "drawn", "combination", "line")
            has_single = any(isinstance(data.get(key), (list, tuple)) for key in single_keys)
            has_columns = any(f"n{position}" in data for position in range(1, 7))
            if has_single or has_columns:
                data = [data]
            else:
                raise TypeError("JSON must be an array or contain a draws/rows/results array.")
    if isinstance(data, list) and data and all(isinstance(item, (int, str)) for item in data):
        data = [data]
    if not isinstance(data, list):
        raise TypeError("JSON must contain an array of draw rows.")
    records: list[tuple[tuple[int, ...], str]] = []
    for item in data:
        numbers: tuple[int, ...] | None = None
        draw_date = ""
        if isinstance(item, (list, tuple)):
            numbers = pick_six(item)
        elif isinstance(item, dict):
            for key in ("n", "numbers", "zahlen", "main", "drawn", "combination", "line"):
                value = item.get(key)
                if isinstance(value, (list, tuple)):
                    numbers = pick_six(value)
                    break
            if numbers is None:
                values = [
                    item.get(f"n{position}") or item.get(f"number{position}") or item.get(f"zahl{position}")
                    for position in range(1, 7)
                ]
                numbers = pick_six(values)
            draw_date = normalize_draw_date(
                item.get("d") or item.get("date") or item.get("datum") or item.get("draw_date") or ""
            )
        if numbers:
            records.append((numbers, draw_date))
    if not records:
        raise ValueError("No valid six-number rows found in the JSON.")
    return records


def parse_data_text(text: str, header_mode: str = "auto") -> list[tuple[tuple[int, ...], str]]:
    stripped = text.strip()
    if not stripped:
        raise ValueError("The file is empty.")
    if stripped[0] in "[{":
        return parse_json_text(stripped)
    return parse_csv_text(stripped, header_mode=header_mode)


def read_data_file(path: str | Path, header_mode: str = "auto") -> list[tuple[tuple[int, ...], str]]:
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise ValueError(f"File not found: {file_path}")
    return parse_data_text(file_path.read_text(encoding="utf-8-sig"), header_mode=header_mode)


def comparable_draws(records: list[tuple[tuple[int, ...], str]]) -> list[tuple[tuple[int, ...], str]]:
    """Drop legacy Wednesday B-draw rows before the modern Wednesday schedule began."""
    selected: list[tuple[tuple[int, ...], str]] = []
    cutoff = date(2000, 12, 2)
    for numbers, draw_date in records:
        try:
            parsed = date.fromisoformat(draw_date)
        except ValueError:
            selected.append((numbers, draw_date))
            continue
        if parsed.weekday() == 2 and parsed < cutoff:
            continue
        selected.append((numbers, draw_date))
    return selected


def load_default_archive() -> list[tuple[tuple[int, ...], str]]:
    if not DEFAULT_ARCHIVE.is_file():
        return []
    return comparable_draws(read_data_file(DEFAULT_ARCHIVE))


def import_draws(path: str | Path, append: bool = False, keep_legacy: bool = False, header_mode: str = "auto") -> None:
    records = read_data_file(path, header_mode=header_mode)
    if not keep_legacy:
        records = comparable_draws(records)
    with closing(database_connection()) as connection, connection:
        if not append:
            connection.execute("DELETE FROM draw")
        connection.executemany(
            "INSERT INTO draw(numbers_json, draw_date, source) VALUES (?, ?, ?)",
            [(json.dumps(numbers), draw_date, str(path)) for numbers, draw_date in records],
        )
    first_date, last_date = draw_date_range(records)
    range_text = f" ({first_date} to {last_date})" if first_date else ""
    print(f"Imported {len(records):,} draw rows from {path}{range_text}.")


def import_players(path: str | Path, append: bool = False) -> None:
    records = read_data_file(path)
    with closing(database_connection()) as connection, connection:
        if not append:
            connection.execute("DELETE FROM player_ticket")
        created_at = datetime.now(UTC).isoformat()
        connection.executemany(
            "INSERT INTO player_ticket(numbers_json, created_at) VALUES (?, ?)",
            [(json.dumps(numbers), created_at) for numbers, _ in records],
        )
    print(f"Imported {len(records):,} player-selected lines from {path}.")


def print_ticket_analysis(numbers: tuple[int, ...]) -> None:
    analysis = analyze_ticket(numbers)
    counts, draw_count, source = frequency_counts()
    historical = {number: count for number, count in enumerate(counts, 1)}
    expected, sigma = frequency_expectation(draw_count)
    member_tickets = "ticket" if analysis["sum_count"] == 1 else "tickets"
    print(f"\nLOTTO 6aus49 ticket: {' '.join(f'{number:2}' for number in numbers)}\n{'=' * 64}")
    print(f"Exact ticket odds     1 in {TOTAL_COMBINATIONS:,}  (same for every ticket)")
    typical_score, _ = score_ticket(numbers, "typical")
    anti_score, _ = score_ticket(numbers, "anti-sharing")
    print(f"Pattern scores        typical: {typical_score:.1f}/100 | anti-sharing: {anti_score:.1f}/100")
    print("Anti-sharing rewards rare parity by assumption; pattern flags remain descriptive.")
    print(f"Sum                   {analysis['sum']}  | {analysis['sum_count']:,} {member_tickets} | {one_in(analysis['sum_count'])}")
    print(f"Odd / even            {analysis['odd_count']} / {analysis['even_count']}  | {analysis['parity_count'] / TOTAL_COMBINATIONS:.2%} of all tickets")
    print(f"Adjacent pairs        {analysis['adjacent_count']}  | longest run: {analysis['longest_run']}")
    if analysis["longest_run"] == 6:
        print(f"Consecutive run       all six consecutive | {CONSECUTIVE_SIX} tickets | {one_in(CONSECUTIVE_SIX)}")
    low_count = math.comb(31, 6)
    birthday = "all numbers 1–31" if analysis["all_low"] else "includes at least one number over 31"
    print(f"Birthday range        {birthday} | all-1–31 category: {low_count / TOTAL_COMBINATIONS:.2%}")
    print(f"\nHistorical snapshot ({draw_count:,} draws from {source}; descriptive only)")
    for number in numbers:
        count = historical[number]
        print(f"  {number:2}: {count:>4} appearances ({(count - expected) / sigma:+.2f}σ from expected)")
    print("\nSharing-risk flags (qualitative, not a draw probability)")
    for level, message in assess_ticket(numbers):
        print(f"  [{level.upper()}] {message}")
    print("These flags concern possible ticket popularity and prize sharing only.")
    print("Every exact ticket still has precisely the same draw probability.")
    print("Past frequencies do not make a number due or predict the next fair draw.")


def print_assessment(numbers: list[int]) -> None:
    ordered = validate_numbers(numbers)
    print(f"\nTicket: {' '.join(f'{number:2}' for number in ordered)}")
    print(f"Exact draw probability: 1 in {TOTAL_COMBINATIONS:,} (unchanged by any pattern)")
    typical_score, _ = score_ticket(ordered, "typical")
    anti_score, _ = score_ticket(ordered, "anti-sharing")
    print(f"Pattern scores: typical {typical_score:.1f}/100 | anti-sharing {anti_score:.1f}/100")
    print("Anti-sharing rewards rare parity by assumption; pattern flags remain descriptive.")
    for level, message in assess_ticket(ordered):
        print(f"  [{level.upper()}] {message}")
    print("\nThese are popularity/sharing heuristics, not a probability of winning.")


def print_status() -> None:
    _counts, draw_count, source = frequency_counts()
    snapshot_draws, snapshot_rows = load_snapshot()
    draws = load_draws()
    first_date, last_date = draw_date_range(draws)
    with closing(database_connection()) as connection:
        saved_count = connection.execute("SELECT COUNT(*) FROM saved_ticket").fetchone()[0]
        player_count = connection.execute("SELECT COUNT(*) FROM player_ticket").fetchone()[0]
    print(f"\nLOTTO 6aus49 data status\n{'=' * 72}")
    print(f"Active frequency source: {source} ({draw_count:,} draws)")
    print(
        f"Snapshot: {snapshot_draws:,} draws | updated: "
        f"{snapshot_rows[0]['updated_at'] if snapshot_rows else 'unknown'}"
    )
    print(f"Row-level archive: {len(draws):,} draws ({first_date or 'unknown'} to {last_date or 'unknown'})")
    print(f"User-imported draw rows: {user_imported_draw_count():,}")
    print(f"Saved tickets: {saved_count:,} | player-selection lines: {player_count:,}")


def print_report() -> None:
    _counts, draw_count, source = frequency_counts()
    expected, sigma = frequency_expectation(draw_count)
    draws = load_draws()
    date_min, date_max = draw_date_range(draws) if source == "imported draw archive" else ("", "")
    odd_even = math.comb(25, 3) * math.comb(24, 3)
    range_text = f" ({date_min} to {date_max})" if date_min else ""
    updated = snapshot_updated_at()
    updated_text = f" | snapshot updated: {updated}" if updated else ""
    print(f"\nGerman LOTTO 6aus49 — probability report\n{'=' * 64}")
    print(f"Data: {source} · {draw_count:,} draws{range_text}{updated_text}")
    print(f"Possible six-number tickets: {TOTAL_COMBINATIONS:,}")
    print(f"Every exact ticket: {format_odds(TOTAL_COMBINATIONS)}")
    print(f"Jackpot (6 + Superzahl): {format_odds(TOTAL_COMBINATIONS * 10)}")
    print(f"Any prize class: about {format_odds(1 / any_prize_probability())}")
    print(f"Expected ticket sum: {SUM_MEAN:.0f} | standard deviation: {SUM_SIGMA:.2f}")
    print(f"3 odd + 3 even tickets: {odd_even:,} ({odd_even / TOTAL_COMBINATIONS:.2%})")
    print(f"All six consecutive tickets: {CONSECUTIVE_SIX} ({one_in(CONSECUTIVE_SIX)})")
    print(f"Expected appearances/number: {expected:.2f} | σ: {sigma:.2f}")
    print("\nCommands: analyze, assess, random, best, status, history, drift, backtest, patterns, pairs, prizes, ev, players, import-draws, import-players, export")


def print_history() -> None:
    counts, draw_count, source = frequency_counts()
    expected, sigma = frequency_expectation(draw_count)
    adjusted, p_value = adjusted_chi_square(counts, draw_count)
    pearson = pearson_chi_square(counts, draw_count)
    draws = load_draws()
    date_min, date_max = draw_date_range(draws) if source == "imported draw archive" else ("", "")
    z_scores = [(count - expected) / sigma for count in counts]
    ranked = sorted(range(1, 50), key=lambda number: counts[number - 1], reverse=True)
    range_text = f" | {date_min} to {date_max}" if date_min else ""
    updated = snapshot_updated_at()
    updated_text = f" | snapshot updated: {updated}" if updated else ""
    verdict = (
        "A global deviation is flagged at the 1% level; investigate data quality or process."
        if p_value < 0.01
        else "No significant global deviation detected under the fair 6/49 model."
    )
    print(f"\nHistorical draw-frequency and fairness audit\n{'=' * 64}")
    print(f"Source: {source} | draws: {draw_count:,}{range_text}{updated_text}")
    print(f"Expected appearances/number: {expected:.2f} | σ: {sigma:.2f}")
    print(f"Pearson χ² (descriptive only): {pearson:.2f} | adjusted χ²: {adjusted:.2f} | df: 48 | p: {p_value:.4f}")
    print(f"Verdict: {verdict}")
    print("\nMost frequently drawn")
    for number in ranked[:10]:
        print(f"  {number:02}: {counts[number - 1]:>4} ({(counts[number - 1] - expected) / sigma:+.2f}σ)")
    print("\nLeast frequently drawn")
    for number in ranked[-10:][::-1]:
        print(f"  {number:02}: {counts[number - 1]:>4} ({(counts[number - 1] - expected) / sigma:+.2f}σ)")
    print("\nAll 49 numbers (Bonferroni-adjusted individual p across 49 tests)")
    print("  no. | drawn | expected | difference | z-score | adj. p")
    for number, count, z_score in zip(range(1, 50), counts, z_scores):
        adjusted_p = bonferroni(normal_two_sided_p(z_score), 49)
        print(f"  {number:02} | {count:5} | {expected:8.2f} | {count - expected:+10.2f} | {z_score:+7.2f} | {adjusted_p:.4f}")
    print("\nThe adjusted statistic accounts for the negative dependence within each 6/49 draw.")
    print("Historical fluctuations are descriptive, not predictive.")


def print_pattern_theory() -> None:
    rows = [
        ("Exactly 3 odd + 3 even", math.comb(25, 3) * math.comb(24, 3)),
        ("Exactly 2 odd + 4 even", math.comb(25, 2) * math.comb(24, 4)),
        ("Exactly 4 odd + 2 even", math.comb(25, 4) * math.comb(24, 2)),
        ("All six numbers ≤31", math.comb(31, 6)),
        ("Six consecutive numbers", CONSECUTIVE_SIX),
        ("Sum exactly 150", SUM_COUNTS[150]),
    ]
    print("\nTheoretical pattern categories")
    print("  pattern | combinations | probability")
    for name, count in rows:
        print(f"  {name:28} | {count:10,} | {count / TOTAL_COMBINATIONS:.4%}")


def print_distribution(title: str, observed: list[int], theoretical: list[int], draw_count: int) -> None:
    expected = [count / TOTAL_COMBINATIONS * draw_count for count in theoretical]
    _statistic, degrees_of_freedom, p_value = weighted_chi_square(observed, expected)
    print(f"\n{title}")
    print("  value | observed | expected | difference")
    for value, (observed_count, expected_count) in enumerate(zip(observed, expected)):
        print(f"  {value:5} | {observed_count:8} | {expected_count:8.2f} | {observed_count - expected_count:+8.2f}")
    print(f"  chi-square p = {p_value:.4f} (df={degrees_of_freedom}; expected bins below 5 merged)")


def print_patterns() -> None:
    draws = load_draws()
    if not draws:
        print("\nNo row-level draw archive available; showing theoretical distributions only.")
        print_pattern_theory()
        return
    observed = observed_patterns(draws)
    draw_count = len(draws)
    print(f"\nObserved pattern distributions\n{'=' * 64}")
    print(f"Draws: {draw_count:,} | source: row-level draw archive")
    print_distribution("Odd numbers in draw", observed["odd"], odd_even_distribution(), draw_count)  # type: ignore[arg-type]
    print_distribution("Numbers 1–24 in draw", observed["low"], low_high_distribution(), draw_count)  # type: ignore[arg-type]
    print_distribution("Numbers 1–31 in draw (birthday range)", observed["birthday"], birthday_distribution(), draw_count)  # type: ignore[arg-type]
    print_distribution("Adjacent-number pairs in draw", observed["adjacency"], adjacency_distribution(), draw_count)  # type: ignore[arg-type]
    sum_observed, sum_expected = sum_bin_counts(observed["sum_counts"], draw_count)  # type: ignore[arg-type]
    _sum_statistic, sum_df, sum_p = weighted_chi_square(sum_observed, sum_expected)
    print("\nSum of six numbers")
    print(f"  observed mean {observed['sum_mean']:.2f} | sd {observed['sum_sd']:.2f} | median {observed['sum_median']}")
    print(f"  theory   mean {SUM_MEAN:.2f} | sd {SUM_SIGMA:.2f}")
    print(f"  binned chi-square p = {sum_p:.4f} (df={sum_df})")
    range_observed, range_expected = range_bin_counts(observed["range_counts"], draw_count)  # type: ignore[arg-type]
    _range_statistic, range_df, range_p = weighted_chi_square(range_observed, range_expected)
    theoretical_range_mean = sum(index * count for index, count in enumerate(range_distribution())) / TOTAL_COMBINATIONS
    print("\nRange (maximum minus minimum)")
    print(f"  observed mean {observed['range_mean']:.2f} | theory {theoretical_range_mean:.2f}")
    print(f"  binned chi-square p = {range_p:.4f} (df={range_df})")
    theoretical_adjacency_mean = sum(index * count for index, count in enumerate(adjacency_distribution())) / TOTAL_COMBINATIONS
    print("\nAdjacency check")
    print(f"  observed mean {observed['adjacency_mean']:.3f} | theory {theoretical_adjacency_mean:.3f}")
    print("\nA p-value is descriptive: many patterns are examined, so isolated small p-values are expected.")
    print_pattern_theory()


def format_combination(combination: object) -> str:
    return ",".join(str(number) for number in combination)  # type: ignore[union-attr]


def print_pairs() -> None:
    draws = load_draws()
    if not draws:
        raise ValueError("No row-level draws imported. Run `python3 app.py import-draws PATH` first.")
    for size, label in ((2, "pairs"), (3, "triples")):
        statistics = combination_statistics(draws, size)
        print(f"\nMost frequent {label}\n{'=' * 64}")
        print(f"Family size: {statistics['family']:,} | expected per combination: {statistics['expected']:.2f} | σ: {statistics['standard_deviation']:.2f}")
        print("  combination | observed | z-score | exact binomial p | Bonferroni p")
        for item in statistics["top"]:  # type: ignore[union-attr]
            print(f"  {format_combination(item['combination']):11} | {item['observed']:8} | {item['z_score']:+7.2f} | {item['p_value']:.4f} | {item['adjusted_p']:.4f}")
        print(f"\nLeast frequent {label}")
        for item in statistics["bottom"]:  # type: ignore[union-attr]
            print(f"  {format_combination(item['combination']):11} | {item['observed']:8} | {item['z_score']:+7.2f} | {item['p_value']:.4f} | {item['adjusted_p']:.4f}")
        if size == 3:
            print(f"\nUnique observed triples: {statistics['unique']:,} | never observed: {statistics['never']:,} | expected never: {statistics['expected_never']:.2f}")
        print("Exact-binomial extremes are exploratory; the Bonferroni column adjusts across the whole family.")


def print_players() -> None:
    tickets = load_player_tickets()
    if not tickets:
        print("\nNo player-selection data imported. Run `python3 app.py import-players PATH` first.")
        print("Without real ticket data, popularity claims would be speculation.")
        return
    summary = player_summary(tickets)
    print(f"\nPlayer-selection analysis\n{'=' * 64}")
    print(f"Ticket lines: {summary['tickets']:,} | slots: {summary['slots']:,} | expected per number: {summary['expected_slots_per_number']:.2f}")
    print(f"Birthday-range slots (1–31): {summary['birthday_slots']:,} ({summary['birthday_share']:.1%})")
    print(f"Distinct combinations: {summary['distinct']:,} | duplicate entries beyond first copy: {summary['duplicate_entries']:,}")
    print("\nMost selected numbers in this sample")
    for item in summary["top"]:  # type: ignore[union-attr]
        print(f"  {item['number']:02}: {item['count']:,}")
    print("\nRepeated combinations in this sample")
    if summary["duplicates"]:  # type: ignore[union-attr]
        for item in summary["duplicates"]:  # type: ignore[union-attr]
            print(f"  {format_combination(item['numbers'])}: {item['count']}×")
    else:
        print("  No duplicate six-number lines in the sample.")
    print("\nThis describes only the uploaded sample and does not change draw probabilities.")


def build_results() -> dict[str, object]:
    counts, draw_count, source = frequency_counts()
    expected, sigma = frequency_expectation(draw_count)
    adjusted, p_value = adjusted_chi_square(counts, draw_count)
    draws = load_draws()
    date_min, date_max = draw_date_range(draws)
    results: dict[str, object] = {
        "game": "German LOTTO 6aus49",
        "source": source,
        "draws": draw_count,
        "fairness_draws": draw_count,
        "row_archive_draws": len(draws),
        "row_archive_source": (
            "imported draw archive" if user_imported_draw_count() else "bundled archive"
        ) if draws else "none",
        "date_range": {"from": date_min, "to": date_max},
        "snapshot_updated_at": snapshot_updated_at(),
        "fairness": {
            "counts": counts,
            "expected_per_number": expected,
            "standard_deviation": sigma,
            "pearson_chi_square": pearson_chi_square(counts, draw_count),
            "pearson_note": "Raw Pearson chi-square is descriptive; the adjusted statistic accounts for within-draw dependence.",
            "adjusted_chi_square": adjusted,
            "degrees_of_freedom": 48,
            "p_value": p_value,
            "verdict": "potential global deviation" if p_value < 0.01 else "no significant global deviation",
        },
    }
    if draws:
        results["patterns"] = observed_patterns(draws)
        results["pairs"] = combination_statistics(draws, 2)
        results["triples"] = combination_statistics(draws, 3)
    tickets = load_player_tickets()
    if tickets:
        results["players"] = player_summary(tickets)
    return results


def export_csv_rows(results: dict[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    fairness = results["fairness"]  # type: ignore[index]
    fairness_draw_count = int(results["draws"])  # type: ignore[arg-type]
    expected = fairness["expected_per_number"]  # type: ignore[index]
    sigma = fairness["standard_deviation"]  # type: ignore[index]
    rows.append({
        "section": "metadata", "item": "fairness_draws", "observed": fairness_draw_count,
        "expected": "", "z_score": "", "p_value": "", "adjusted_p": "",
        "notes": "Draw count used for number-frequency/fairness statistics.",
    })
    rows.append({
        "section": "metadata", "item": "snapshot_updated_at",
        "observed": results.get("snapshot_updated_at", ""),
        "expected": "", "z_score": "", "p_value": "", "adjusted_p": "",
        "notes": "Timestamp of the stored LOTTO.de snapshot.",
    })
    if "patterns" in results:
        rows.append({
            "section": "metadata", "item": "row_archive_draws",
            "observed": int(results["patterns"]["draws"]),  # type: ignore[index]
            "expected": "", "z_score": "", "p_value": "", "adjusted_p": "",
            "notes": "Draw count used for patterns, pairs and triples.",
        })
    for number, count in enumerate(fairness["counts"], 1):  # type: ignore[index]
        z_score = (count - expected) / sigma
        p_value = normal_two_sided_p(z_score)
        rows.append({
            "section": "number_frequency", "item": str(number), "observed": count,
            "expected": round(expected, 6), "z_score": round(z_score, 6),
            "p_value": round(p_value, 8), "adjusted_p": round(bonferroni(p_value, 49), 8),
            "notes": "Adjusted p is Bonferroni across 49 numbers.",
        })
    rows.append({
        "section": "global_fairness", "item": "adjusted_chi_square",
        "observed": round(fairness["adjusted_chi_square"], 6),  # type: ignore[index]
        "expected": 48, "z_score": "", "p_value": round(fairness["p_value"], 8),  # type: ignore[index]
        "adjusted_p": "", "notes": fairness["verdict"],  # type: ignore[index]
    })
    if "patterns" in results:
        patterns = results["patterns"]  # type: ignore[index]
        pattern_draw_count = int(patterns["draws"])  # type: ignore[index]
        for key, title, theoretical in (
            ("odd", "odd_count", odd_even_distribution()),
            ("low", "low_count_1_24", low_high_distribution()),
            ("birthday", "birthday_count_1_31", birthday_distribution()),
            ("adjacency", "adjacent_pairs", adjacency_distribution()),
        ):
            for value, (obs, exp_count) in enumerate(zip(patterns[key], [count / TOTAL_COMBINATIONS * pattern_draw_count for count in theoretical])):  # type: ignore[index]
                rows.append({
                    "section": title, "item": str(value), "observed": obs,
                    "expected": round(exp_count, 6), "z_score": "", "p_value": "",
                    "adjusted_p": "", "notes": "Observed versus fair-model expectation using row-archive draws.",
                })
        sum_observed, sum_expected = sum_bin_counts(patterns["sum_counts"], pattern_draw_count)  # type: ignore[index]
        sum_stat, sum_df, sum_p = weighted_chi_square(sum_observed, sum_expected)
        rows.append({
            "section": "sum_distribution", "item": "5-point binned chi-square",
            "observed": round(sum_stat, 6), "expected": 0, "z_score": "",
            "p_value": round(sum_p, 8), "adjusted_p": "",
            "notes": f"df={sum_df}; observed mean={patterns['sum_mean']:.4f}; theory mean={SUM_MEAN}.",
        })
        range_observed, range_expected = range_bin_counts(patterns["range_counts"], pattern_draw_count)  # type: ignore[index]
        range_stat, range_df, range_p = weighted_chi_square(range_observed, range_expected)
        rows.append({
            "section": "range_distribution", "item": "5-point binned chi-square",
            "observed": round(range_stat, 6), "expected": 0, "z_score": "",
            "p_value": round(range_p, 8), "adjusted_p": "",
            "notes": f"df={range_df}; observed mean={patterns['range_mean']:.4f}.",
        })
    for key, label in (("pairs", "pair"), ("triples", "triple")):
        if key not in results:
            continue
        statistics = results[key]  # type: ignore[index]
        for group, items in (("highest_observed", statistics["top"]), ("lowest_observed", statistics["bottom"])):  # type: ignore[index]
            for item in items:
                rows.append({
                    "section": f"{group}_{label}s", "item": format_combination(item["combination"]),
                    "observed": item["observed"], "expected": round(item["expected"], 6),
                    "z_score": round(item["z_score"], 6), "p_value": round(item["p_value"], 8),
                    "adjusted_p": round(item["adjusted_p"], 8),
                    "notes": f"Family size {statistics['family']}; expected per combination {statistics['expected']:.2f}.",
                })
    if "players" in results:
        players = results["players"]  # type: ignore[index]
        rows.append({
            "section": "player_selection", "item": "ticket_lines", "observed": players["tickets"],
            "expected": "", "z_score": "", "p_value": "", "adjusted_p": "",
            "notes": f"Distinct={players['distinct']}; birthday slot share={players['birthday_share']:.4f}.",
        })
        for item in players["top"]:  # type: ignore[union-attr]
            rows.append({
                "section": "player_selection", "item": f"number_{item['number']}",
                "observed": item["count"], "expected": round(players["expected_slots_per_number"], 6),
                "z_score": "", "p_value": "", "adjusted_p": "",
                "notes": "Selected-number frequency in the uploaded sample.",
            })
    return rows


def export_results(path: str | Path, fmt: str) -> None:
    results = build_results()
    output = Path(path).expanduser()
    if fmt == "json":
        output.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    elif fmt == "csv":
        rows = export_csv_rows(results)
        with output.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=[
                "section", "item", "observed", "expected", "z_score", "p_value", "adjusted_p", "notes",
            ])
            writer.writeheader()
            writer.writerows(rows)
    else:
        raise ValueError(f"Unsupported export format: {fmt}")
    print(f"Wrote {fmt.upper()} results to {output}")


def player_popularity() -> dict[str, object]:
    tickets = load_player_tickets()
    if not tickets:
        raise ValueError("No player-selection data imported. Run `python3 app.py import-players PATH` first.")
    counts = [0] * 49
    combinations_count: Counter[tuple[int, ...]] = Counter()
    for ticket in tickets:
        ordered = tuple(sorted(ticket))
        combinations_count[ordered] += 1
        for number in ordered:
            counts[number - 1] += 1
    slots = len(tickets) * 6
    return {
        "tickets": len(tickets),
        "counts": counts,
        "slots": slots,
        "expected": slots / 49,
        "combinations": combinations_count,
    }


def empirical_sharing_score(
    numbers: tuple[int, ...], popularity: dict[str, object]
) -> tuple[float, dict[str, float]]:
    ordered = tuple(sorted(numbers))
    if len(ordered) != 6 or len(set(ordered)) != 6 or any(not 1 <= number <= 49 for number in ordered):
        raise ValueError("A score requires six distinct numbers from 1 through 49.")
    counts = popularity["counts"]
    expected = float(popularity["expected"])
    if expected <= 0:
        raise ValueError("Player-selection sample is empty.")
    relative = (
        sum(int(counts[number - 1]) for number in ordered) / 6 / expected  # type: ignore[index]
    )
    factor = max(0.0, min(1.0, (2.0 - relative) / 2.0))
    duplicate = 0.5 if popularity["combinations"].get(ordered, 0) else 1.0  # type: ignore[union-attr]
    score = 100 * factor * duplicate
    return round(max(0.0, min(100.0, score)), 1), {
        "relative_popularity": round(relative, 4),
        "duplicate_penalty": duplicate,
    }


def save_ticket(numbers: tuple[int, ...], label: str) -> None:
    with closing(database_connection()) as connection, connection:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO saved_ticket(numbers_json, label, created_at) VALUES (?, ?, ?)",
            (json.dumps(numbers), label[:80], datetime.now(UTC).isoformat()),
        )
        inserted = cursor.rowcount > 0
    print(f"{'Saved' if inserted else 'Already saved'} locally: {' '.join(map(str, numbers))}")


def list_tickets() -> None:
    with closing(database_connection()) as connection:
        rows = connection.execute("SELECT numbers_json, label, created_at FROM saved_ticket ORDER BY id DESC").fetchall()
    if not rows:
        print("No tickets saved locally.")
    for row in rows:
        label = f" — {row['label']}" if row["label"] else ""
        print(f"{' '.join(map(str, json.loads(row['numbers_json'])))}{label} ({row['created_at']})")


def print_drift(years: int = 10, window_draws: int = 0, window_step: int = 0) -> None:
    if years < 0:
        raise ValueError("--years must be zero or positive.")
    draws = load_draws()
    if not draws:
        raise ValueError("No row-level draws available for drift analysis.")
    dated = sorted(
        ((draw_date, numbers) for numbers, draw_date in draws if draw_date),
        key=lambda item: item[0],
    )
    if not dated:
        raise ValueError("Drift analysis requires dated draw rows.")
    print(f"\nLOTTO 6aus49 year-by-year drift\n{'=' * 80}")
    print("Year | draws | adjusted χ² | p | max|z| | note (Bonferroni family-wise)")
    available_years = sorted({draw_date[:4] for draw_date, _ in dated})
    selected_years = available_years[-years:] if years > 0 else available_years
    year_results = []
    for year in selected_years:
        rows = [numbers for draw_date, numbers in dated if draw_date.startswith(year)]
        counts = [0] * 49
        for numbers in rows:
            for number in numbers:
                counts[number - 1] += 1
        draw_count = len(rows)
        expected, sigma = frequency_expectation(draw_count)
        adjusted, p_value = adjusted_chi_square(counts, draw_count)
        max_z = max(abs((count - expected) / sigma) for count in counts)
        year_results.append((year, draw_count, adjusted, p_value, max_z))
    for year, draw_count, adjusted, p_value, max_z in year_results:
        note = "significant after correction" if bonferroni(p_value, len(year_results)) < 0.01 else ""
        print(f"{year} | {draw_count:5} | {adjusted:11.2f} | {p_value:.4f} | {max_z:6.2f} | {note}")
    print(f"Bonferroni family size for the yearly scan: {len(year_results)}")
    if window_draws > 0:
        if window_draws < 10:
            raise ValueError("--window-draws must be at least 10.")
        step = window_step if window_step > 0 else window_draws
        if step < 1:
            raise ValueError("--window-step must be positive.")
        print(f"\nWindowed drift (window={window_draws}, step={step})\n{'=' * 80}")
        print("Window | draws | adjusted χ² | p | max|z| | dates | note (Bonferroni family-wise)")
        window_results = []
        for index in range(0, len(dated) - window_draws + 1, step):
            block = dated[index:index + window_draws]
            counts = [0] * 49
            for _draw_date, numbers in block:
                for number in numbers:
                    counts[number - 1] += 1
            draw_count = len(block)
            expected, sigma = frequency_expectation(draw_count)
            adjusted, p_value = adjusted_chi_square(counts, draw_count)
            max_z = max(abs((count - expected) / sigma) for count in counts)
            window_results.append((index // step + 1, draw_count, adjusted, p_value, max_z, block[0][0], block[-1][0]))
        for number, draw_count, adjusted, p_value, max_z, first, last in window_results:
            note = "significant after correction" if bonferroni(p_value, len(window_results)) < 0.01 else ""
            print(
                f"{number:6} | {draw_count:5} | {adjusted:11.2f} | {p_value:.4f} | "
                f"{max_z:6.2f} | {first} to {last} | {note}"
            )
        print(f"Bonferroni family size for the window scan: {len(window_results)}")
    print("Drift scans are exploratory; many windows increase false-positive risk.")


def print_backtest(samples: int = 5000) -> None:
    draws = load_draws()
    if not draws:
        raise ValueError("No row-level draws available for backtesting.")
    if samples <= 0:
        raise ValueError("--samples must be positive.")
    actual = [score_ticket(numbers)[0] for numbers, _date in draws]
    rng = random.Random(20260101)
    random_scores = [
        score_ticket(tuple(sorted(rng.sample(range(1, 50), 6))))[0]
        for _ in range(samples)
    ]
    actual_mean = sum(actual) / len(actual)
    actual_sd = math.sqrt(sum((value - actual_mean) ** 2 for value in actual) / len(actual))
    random_mean = sum(random_scores) / len(random_scores)
    random_sd = math.sqrt(sum((value - random_mean) ** 2 for value in random_scores) / len(random_scores))
    standard_error = math.sqrt(actual_sd ** 2 / len(actual) + random_sd ** 2 / len(random_scores))
    z_score = (actual_mean - random_mean) / standard_error if standard_error else 0.0
    p_value = normal_two_sided_p(z_score)
    print(f"\nLOTTO 6aus49 score calibration backtest (anti-sharing profile)\n{'=' * 72}")
    print(f"Historical winning lines: {len(actual):,} | random lines: {len(random_scores):,}")
    print(f"Actual mean score: {actual_mean:.2f} | sd: {actual_sd:.2f} | median: {sorted(actual)[len(actual) // 2]:.1f}")
    print(f"Random mean score: {random_mean:.2f} | sd: {random_sd:.2f} | median: {sorted(random_scores)[len(random_scores) // 2]:.1f}")
    print(f"Difference: {actual_mean - random_mean:+.2f} | z: {z_score:+.2f} | p: {p_value:.4f}")
    print(f"Actual scores ≥75: {sum(value >= 75 for value in actual) / len(actual):.1%} | "
          f"random: {sum(value >= 75 for value in random_scores) / len(random_scores):.1%}")
    print("This tests score calibration against history; it does not create a predictive edge.")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Terminal-only LOTTO 6aus49 probability and pattern analyzer.")
    parser.add_argument("--offline", action="store_true", help="skip the LOTTO.de statistics refresh")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("report", help="show core Lotto probabilities (default)")
    analyze = commands.add_parser("analyze", help="analyze six numbers")
    analyze.add_argument("numbers", nargs=6, type=int, metavar="N")
    assess = commands.add_parser("assess", help="flag popular/sharing-prone ticket patterns")
    assess.add_argument("numbers", nargs=6, type=int, metavar="N")
    commands.add_parser("random", help="generate and analyze a uniform random ticket")
    best = commands.add_parser("best", help="generate random tickets and rank them by pattern score")
    best.add_argument("--runs", type=int, default=100, help="number of random tickets (default 100)")
    best.add_argument("--top", type=int, default=0, help="show only the top N tickets (0 = all)")
    best.add_argument("--profile", choices=("typical", "anti-sharing", "empirical"), default="anti-sharing", help="scoring profile (default: anti-sharing)")
    commands.add_parser("status", help="show snapshot, archive, and local data status")
    commands.add_parser("history", help="audit number frequencies and global fairness")
    drift = commands.add_parser("drift", help="compare number fairness across years or rolling windows")
    drift.add_argument("--years", type=int, default=10, help="number of recent years (0 = all)")
    drift.add_argument("--window-draws", type=int, default=0, help="rolling window size")
    drift.add_argument("--window-step", type=int, default=0, help="window step (default: window size)")
    backtest = commands.add_parser("backtest", help="compare score distributions of historical winners and random lines")
    backtest.add_argument("--samples", type=int, default=5000, help="random comparison sample size")
    commands.add_parser("patterns", help="compare observed pattern distributions with theory")
    commands.add_parser("pairs", help="analyze historical pairs and triples")
    commands.add_parser("players", help="analyze an imported player-selection sample")
    import_draws_command = commands.add_parser("import-draws", help="import a CSV/JSON draw archive")
    import_draws_command.add_argument("path", help="CSV or JSON file with six main numbers per row")
    import_draws_command.add_argument("--append", action="store_true", help="append instead of replacing the draw table")
    import_draws_command.add_argument("--keep-legacy", action="store_true", help="keep legacy Wednesday B-draw rows")
    import_draws_command.add_argument("--header", choices=("auto", "yes", "no"), default="auto", help="CSV header handling")
    import_players_command = commands.add_parser("import-players", help="import player-selected ticket lines")
    import_players_command.add_argument("path", help="CSV or JSON file with six numbers per line")
    import_players_command.add_argument("--append", action="store_true", help="append instead of replacing the player table")
    commands.add_parser("prizes", help="show official LOTTO 6aus49 prize classes and odds")
    ev = commands.add_parser("ev", help="estimate expected value from supplied prize amounts")
    ev.add_argument("--jackpot", type=float, default=0.0, help="class-1 jackpot amount")
    ev.add_argument("--other-prizes", default="", help="comma-separated class 2-9 prize amounts")
    ev.add_argument("--stake", type=float, default=1.0, help="ticket stake/price (default 1.0)")
    export = commands.add_parser("export", help="write a JSON or CSV report")
    export.add_argument("output", help="output file path")
    export.add_argument("--format", choices=("json", "csv"), default="json", help="report format")
    commands.add_parser("refresh", help="fetch the LOTTO.de statistics immediately")
    commands.add_parser("reseed", help="reload the bundled historical snapshot into SQLite")
    save = commands.add_parser("save", help="save six numbers locally in SQLite")
    save.add_argument("numbers", nargs=6, type=int, metavar="N")
    save.add_argument("--label", default="", help="optional local label")
    commands.add_parser("saved", help="list locally saved tickets")
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    try:
        initialize_database(scrape=not args.offline and args.command not in {"refresh", "reseed"})
        if args.command in (None, "report"):
            print_report()
        elif args.command == "analyze":
            print_ticket_analysis(validate_numbers(args.numbers))
        elif args.command == "assess":
            print_assessment(args.numbers)
        elif args.command == "random":
            print_ticket_analysis(tuple(sorted(random.SystemRandom().sample(range(1, 50), 6))))
        elif args.command == "best":
            print_ranked_tickets(args.runs, top=args.top, profile=args.profile)
        elif args.command == "status":
            print_status()
        elif args.command == "history":
            print_history()
        elif args.command == "drift":
            print_drift(args.years, args.window_draws, args.window_step)
        elif args.command == "backtest":
            print_backtest(args.samples)
        elif args.command == "patterns":
            print_patterns()
        elif args.command == "pairs":
            print_pairs()
        elif args.command == "players":
            print_players()
        elif args.command == "import-draws":
            import_draws(args.path, append=args.append, keep_legacy=args.keep_legacy, header_mode=args.header)
        elif args.command == "import-players":
            import_players(args.path, append=args.append)
        elif args.command == "prizes":
            print_prizes()
        elif args.command == "ev":
            print_ev(args.jackpot, args.other_prizes, args.stake)
        elif args.command == "export":
            export_results(args.output, args.format)
        elif args.command == "refresh":
            if args.offline:
                raise ValueError("The refresh command requires network access; remove --offline.")
            refresh_snapshot()
        elif args.command == "reseed":
            reseed_snapshot()
        elif args.command == "save":
            save_ticket(validate_numbers(args.numbers), args.label)
        elif args.command == "saved":
            list_tickets()
    except (ValueError, TypeError, OSError, sqlite3.Error) as error:
        raise SystemExit(f"Error: {error}") from error


if __name__ == "__main__":
    main()
