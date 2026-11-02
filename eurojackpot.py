#!/usr/bin/env python3
"""Terminal-only Eurojackpot (5/50 main + 2/12 Euro) analyzer."""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import re
import sqlite3
import sys
import unicodedata
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from itertools import combinations, pairwise
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from stats_common import (
    binomial_two_sided_p,
    bonferroni,
    chi2_sf,
    normal_two_sided_p,
    weighted_chi_square,
)

ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "eurojackpot.db"
DEFAULT_FULL_COUNTS = ROOT / "data" / "eurojackpot_full.json"
DEFAULT_CURRENT_COUNTS = ROOT / "data" / "eurojackpot_current.json"
DEFAULT_DRAWS = ROOT / "data" / "eurojackpot_draws.csv"
LIVE_STATISTICS_URL = "https://www.lotto-hessen.de/getEurojackpotStatisticsCounts"
WESTLOTTO_STATISTICS_URL = "https://www.westlotto.de/eurojackpot/statistiken/wieoftgezogen.html"
DRAW_YEARS_URL = "https://www.lotto-hessen.de/getDrawYears?game=eurojackpot"
DRAW_KEYS_URL = "https://www.lotto-hessen.de/getEurojackpotNumberDrawsForYear?year={year}"
DRAW_NUMBERS_URL = "https://www.lotto-hessen.de/getEurojackpotHistoricNumbers?gckey={key}"
OFFICIAL_DRAW_SOURCE = "LOTTO Hessen official historic numbers"
PUBLIC_STATISTICS_PAGE = "https://www.lotto-hessen.de/eurojackpot/gewinnzahlen-quoten/statistiken"
SOURCE_NOTE = "LOTTO Hessen Eurojackpot statistics"
EURO_CURRENT_START = "2022-03-25"
EUROJACKPOT_START = "2012-03-23"
SNAPSHOT_VERSION = "eurojackpot-main-full-euro-2022"
EURO_PRIZE_CLASSES = (
    (1, "5 + 2", 139_838_160, "36.00%"),
    (2, "5 + 1", 6_991_908, "8.60%"),
    (3, "5 + 0", 3_107_515, "4.85%"),
    (4, "4 + 2", 621_503, "0.80%"),
    (5, "4 + 1", 31_075, "1.00%"),
    (6, "3 + 2", 14_125, "1.10%"),
    (7, "4 + 0", 13_811, "0.80%"),
    (8, "2 + 2", 985, "2.55%"),
    (9, "3 + 1", 706, "2.85%"),
    (10, "3 + 0", 314, "5.40%"),
    (11, "1 + 2", 188, "6.75%"),
    (12, "2 + 1", 49, "20.30%"),
)
EURO_PRIZE_COUNTS = (
    1, 20, 45, 225, 4_500, 9_900, 10_125, 141_900, 198_000, 445_500, 744_975, 2_838_000,
)
EURO_PRIZE_FULL_OUTCOMES = math.comb(50, 5) * math.comb(12, 2)
EURO_PRIZE_SOURCE = "https://www.lotto.de/eurojackpot/gewinnwahrscheinlichkeit"
MAIN_PICK = 5
MAIN_MAX = 50
EURO_PICK = 2
EURO_MAX = 12
MAIN_COMBINATIONS = math.comb(MAIN_MAX, MAIN_PICK)
EURO_COMBINATIONS = math.comb(EURO_MAX, EURO_PICK)
FULL_COMBINATIONS = MAIN_COMBINATIONS * EURO_COMBINATIONS
LOW_MAX = 25
BIRTHDAY_MAX = 31

try:
    BERLIN = ZoneInfo("Europe/Berlin")
except ZoneInfoNotFoundError:  # pragma: no cover - platform tzdata fallback
    BERLIN = UTC


def database_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def get_metadata(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else None


def set_metadata(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)", (key, value))


def strict_int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer.")  # noqa: TRY004
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    raise ValueError(f"{label} must be an integer.")


def timestamp_to_date(value: object) -> str:
    if value is None:
        return ""
    milliseconds = strict_int(value, "timestamp")
    return datetime.fromtimestamp(milliseconds / 1000, BERLIN).date().isoformat()


def parse_number_entries(entries: object, maximum: int, label: str) -> list[int]:
    if not isinstance(entries, list):
        raise TypeError(f"Eurojackpot statistics {label} list is missing.")
    counts_by_number: dict[int, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise TypeError(f"Eurojackpot statistics {label} entry is not an object.")
        try:
            number = strict_int(entry["number"], "number")
            count = strict_int(entry["value"], "value")
        except KeyError as error:
            raise ValueError(f"Eurojackpot statistics {label} entry is incomplete.") from error
        if not 1 <= number <= maximum or count < 0:
            raise ValueError(f"Eurojackpot statistics {label} contain an out-of-range value.")
        if number in counts_by_number:
            raise ValueError(f"Eurojackpot statistics {label} contain a duplicate number.")
        counts_by_number[number] = count
    if sorted(counts_by_number) != list(range(1, maximum + 1)):
        raise ValueError(f"Eurojackpot statistics {label} do not contain all numbers.")
    return [counts_by_number[number] for number in range(1, maximum + 1)]


def parse_counts_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise TypeError("Eurojackpot statistics response is not a JSON object.")
    main = parse_number_entries(payload.get("numbers"), MAIN_MAX, "main")
    euro = parse_number_entries(payload.get("additionalNumbers"), EURO_MAX, "Euro")
    draw_count = strict_int(payload.get("drawCount"), "drawCount")
    if draw_count <= 0:
        raise ValueError("Eurojackpot statistics contain no draws.")
    if sum(main) != draw_count * MAIN_PICK:
        raise ValueError("Eurojackpot main-number total does not match drawCount.")
    if sum(euro) != draw_count * EURO_PICK:
        raise ValueError("Eurojackpot Euro-number total does not match drawCount.")
    return {
        "main": main,
        "euro": euro,
        "draw_count": draw_count,
        "start_date": timestamp_to_date(payload.get("startDate")),
        "end_date": timestamp_to_date(payload.get("endDate")),
        "source_note": SOURCE_NOTE,
    }


def fetch_lotto_hessen_stats(startdate: str | None = None, timeout: float = 8.0) -> dict[str, object]:
    url = LIVE_STATISTICS_URL
    if startdate:
        url = f"{url}?{urlencode({'startdate': startdate})}"
    request = Request(
        url,
        headers={
            "User-Agent": "LottoAnalyzer/1.0 (educational; Python stdlib)",
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return parse_counts_payload(payload)


def parse_westlotto_html(text: str) -> dict[str, object]:
    main_match = re.search(r"5 aus 50</p>(.*?)2 aus 12</p>", text, re.DOTALL)
    if not main_match:
        raise ValueError("WestLotto page has no 5 aus 50 section.")
    euro_index = text.find("2 aus 12</p>")
    if euro_index < 0:
        raise ValueError("WestLotto page has no 2 aus 12 section.")
    pair_pattern = re.compile(
        r'<span class="polygon-label">(\d+)</span>\s*</span>\s*'
        r'<div class="mod-barometer__label">(\d+)</div>',
        re.DOTALL,
    )
    main_pairs = pair_pattern.findall(main_match.group(1))
    euro_pairs = pair_pattern.findall(text[euro_index:])
    if len(main_pairs) < MAIN_MAX or len(euro_pairs) < EURO_MAX:
        raise ValueError("WestLotto page has incomplete number tables.")
    main = [0] * MAIN_MAX
    euro = [0] * EURO_MAX
    main_seen: set[int] = set()
    euro_seen: set[int] = set()
    for label, count in main_pairs[:MAIN_MAX]:
        number = int(label)
        value = int(count)
        if not 1 <= number <= MAIN_MAX or number in main_seen:
            raise ValueError("WestLotto main-number table is invalid.")
        main_seen.add(number)
        main[number - 1] = value
    for label, count in euro_pairs[:EURO_MAX]:
        number = int(label)
        value = int(count)
        if not 1 <= number <= EURO_MAX or number in euro_seen:
            raise ValueError("WestLotto Euro-number table is invalid.")
        euro_seen.add(number)
        euro[number - 1] = value
    if len(main_seen) != MAIN_MAX or len(euro_seen) != EURO_MAX:
        raise ValueError("WestLotto number tables are incomplete.")
    if sum(main) % MAIN_PICK != 0 or sum(euro) % EURO_PICK != 0:
        raise ValueError("WestLotto totals are not divisible by the pick counts.")
    date_match = re.search(r"Stand:\s*<span>\s*(\d{2})\.(\d{2})\.(\d{4})\s*</span>", text)
    date = f"{date_match.group(3)}-{date_match.group(2)}-{date_match.group(1)}" if date_match else ""
    return {
        "main": main,
        "euro": euro,
        "draw_count": sum(main) // MAIN_PICK,
        "start_date": EUROJACKPOT_START,
        "end_date": date,
        "source_note": "WestLotto Eurojackpot statistics",
    }


def fetch_westlotto_stats(timeout: float = 8.0) -> dict[str, object]:
    request = Request(
        WESTLOTTO_STATISTICS_URL,
        headers={
            "User-Agent": "LottoAnalyzer/1.0 (educational; Python stdlib)",
            "Accept": "text/html",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        text = response.read().decode("utf-8", errors="replace")
    return parse_westlotto_html(text)


def combine_stats(
    main_stats: dict[str, object],
    euro_stats: dict[str, object],
    main_source: str,
    euro_source: str,
    cross_check: str = "",
) -> dict[str, object]:
    main_draw_count = int(main_stats["draw_count"])
    euro_draw_count = int(euro_stats["draw_count"])
    return {
        "main": main_stats["main"],
        "euro": euro_stats["euro"],
        "main_draw_count": main_draw_count,
        "euro_draw_count": euro_draw_count,
        "main_start_date": str(main_stats.get("start_date", "")),
        "main_end_date": str(main_stats.get("end_date", "")),
        "euro_start_date": str(euro_stats.get("start_date", "")),
        "euro_end_date": str(euro_stats.get("end_date", "")),
        "source_note": f"{main_source} for main; {euro_source} for Euro.",
        "cross_check": cross_check,
    }


def fetch_live_stats(timeout: float = 8.0) -> dict[str, object]:
    main_stats: dict[str, object]
    main_source: str
    cross_check = ""
    try:
        main_stats = fetch_lotto_hessen_stats(None, timeout)
        main_source = "LOTTO Hessen full history"
        try:
            westlotto = fetch_westlotto_stats(timeout)
        except (OSError, ValueError, TypeError):
            cross_check = "WestLotto unavailable"
        else:
            cross_check = "matched" if westlotto["main"] == main_stats["main"] else "mismatch"
    except (OSError, ValueError, TypeError) as lotto_error:
        try:
            main_stats = fetch_westlotto_stats(timeout)
            main_source = "WestLotto full history"
            cross_check = "WestLotto fallback"
        except (OSError, ValueError, TypeError) as west_error:
            raise ValueError(
                f"Both Eurojackpot main-number sources failed: {lotto_error}; {west_error}"
            ) from west_error
    euro_stats = fetch_lotto_hessen_stats(EURO_CURRENT_START, timeout)
    if str(euro_stats.get("start_date", "")) < EURO_CURRENT_START:
        raise ValueError(
            f"The Euro-number slice did not honor startdate={EURO_CURRENT_START} "
            f"(reported start {euro_stats.get('start_date')!r})."
        )
    return combine_stats(
        main_stats,
        euro_stats,
        main_source,
        f"LOTTO Hessen Euro numbers since {EURO_CURRENT_START}",
        cross_check,
    )


def load_bundled_stats() -> dict[str, object]:
    if not DEFAULT_FULL_COUNTS.is_file() or not DEFAULT_CURRENT_COUNTS.is_file():
        raise ValueError("Bundled Eurojackpot statistics files are missing.")
    full = parse_counts_payload(json.loads(DEFAULT_FULL_COUNTS.read_text(encoding="utf-8")))
    current = parse_counts_payload(json.loads(DEFAULT_CURRENT_COUNTS.read_text(encoding="utf-8")))
    return combine_stats(
        full,
        current,
        "Bundled LOTTO Hessen full history",
        f"Bundled LOTTO Hessen Euro numbers since {EURO_CURRENT_START}",
        "bundled data",
    )


def write_snapshot(connection: sqlite3.Connection, stats: dict[str, object]) -> None:
    main = stats["main"]
    euro = stats["euro"]
    main_draw_count = int(stats["main_draw_count"])
    euro_draw_count = int(stats["euro_draw_count"])
    if (
        not isinstance(main, list)
        or not isinstance(euro, list)
        or len(main) != MAIN_MAX
        or len(euro) != EURO_MAX
        or main_draw_count <= 0
        or euro_draw_count <= 0
        or sum(main) != main_draw_count * MAIN_PICK
        or sum(euro) != euro_draw_count * EURO_PICK
    ):
        raise ValueError("Invalid Eurojackpot frequency snapshot.")
    updated_at = datetime.now(UTC).isoformat()
    source_note = str(stats.get("source_note", SOURCE_NOTE))
    main_start = str(stats.get("main_start_date", ""))
    main_end = str(stats.get("main_end_date", ""))
    euro_start = str(stats.get("euro_start_date", ""))
    euro_end = str(stats.get("euro_end_date", ""))
    connection.execute("DELETE FROM frequency_snapshot")
    rows = [
        ("main", number, count, source_note, main_draw_count, main_start, main_end, updated_at)
        for number, count in enumerate(main, 1)
    ]
    rows.extend(
        ("euro", number, count, source_note, euro_draw_count, euro_start, euro_end, updated_at)
        for number, count in enumerate(euro, 1)
    )
    connection.executemany(
        "INSERT INTO frequency_snapshot(kind, number, drawn_count, source_note, draw_count, start_date, end_date, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    set_metadata(connection, "snapshot_version", SNAPSHOT_VERSION)
    set_metadata(connection, "last_cross_check", str(stats.get("cross_check", "not recorded")))
    set_metadata(connection, "last_cross_check_at", updated_at)


def store_snapshot(stats: dict[str, object]) -> None:
    with closing(database_connection()) as connection, connection:
        write_snapshot(connection, stats)


def snapshot_is_valid(connection: sqlite3.Connection) -> bool:
    main = connection.execute(
        "SELECT COUNT(*), MIN(number), MAX(number), COUNT(DISTINCT draw_count), "
        "COALESCE(SUM(drawn_count), 0), COALESCE(MAX(draw_count), 0) "
        "FROM frequency_snapshot WHERE kind = 'main'"
    ).fetchone()
    euro = connection.execute(
        "SELECT COUNT(*), MIN(number), MAX(number), COUNT(DISTINCT draw_count), "
        "COALESCE(SUM(drawn_count), 0), COALESCE(MAX(draw_count), 0) "
        "FROM frequency_snapshot WHERE kind = 'euro'"
    ).fetchone()
    main_count, main_min, main_max, main_draw_counts, main_total, main_draws = main
    euro_count, euro_min, euro_max, euro_draw_counts, euro_total, euro_draws = euro
    try:
        version = connection.execute(
            "SELECT value FROM metadata WHERE key = 'snapshot_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    if not version or str(version["value"]) != SNAPSHOT_VERSION:
        return False
    return (
        main_count == MAIN_MAX
        and main_min == 1
        and main_max == MAIN_MAX
        and main_draw_counts == 1
        and main_draws > 0
        and main_total == main_draws * MAIN_PICK
        and euro_count == EURO_MAX
        and euro_min == 1
        and euro_max == EURO_MAX
        and euro_draw_counts == 1
        and euro_draws > 0
        and euro_total == euro_draws * EURO_PICK
    )


def stored_draw_counts(connection: sqlite3.Connection) -> tuple[int, int]:
    main = connection.execute(
        "SELECT MAX(draw_count) FROM frequency_snapshot WHERE kind = 'main'"
    ).fetchone()
    euro = connection.execute(
        "SELECT MAX(draw_count) FROM frequency_snapshot WHERE kind = 'euro'"
    ).fetchone()
    main_count = int(main[0]) if main and main[0] is not None else 0
    euro_count = int(euro[0]) if euro and euro[0] is not None else 0
    return main_count, euro_count


def load_snapshot() -> dict[str, object]:
    with closing(database_connection()) as connection:
        if not snapshot_is_valid(connection):
            raise ValueError("Eurojackpot frequency snapshot is incomplete; run `python3 eurojackpot.py reseed`.")
        rows = connection.execute(
            "SELECT kind, number, drawn_count, source_note, draw_count, start_date, end_date, updated_at "
            "FROM frequency_snapshot ORDER BY kind, number"
        ).fetchall()
    main = [0] * MAIN_MAX
    euro = [0] * EURO_MAX
    main_draw_count = 0
    euro_draw_count = 0
    source_note = ""
    main_start_date = ""
    main_end_date = ""
    euro_start_date = ""
    euro_end_date = ""
    updated_at = ""
    for row in rows:
        source_note = str(row["source_note"])
        updated_at = str(row["updated_at"])
        if row["kind"] == "main":
            main[int(row["number"]) - 1] = int(row["drawn_count"])
            main_draw_count = int(row["draw_count"])
            main_start_date = str(row["start_date"])
            main_end_date = str(row["end_date"])
        else:
            euro[int(row["number"]) - 1] = int(row["drawn_count"])
            euro_draw_count = int(row["draw_count"])
            euro_start_date = str(row["start_date"])
            euro_end_date = str(row["end_date"])
    return {
        "main": main,
        "euro": euro,
        "main_draw_count": main_draw_count,
        "euro_draw_count": euro_draw_count,
        "main_start_date": main_start_date,
        "main_end_date": main_end_date,
        "euro_start_date": euro_start_date,
        "euro_end_date": euro_end_date,
        "source_note": source_note,
        "updated_at": updated_at,
    }


def save_ticket(main: tuple[int, ...], euro: tuple[int, ...], label: str) -> None:
    ordered_main = validate_main(list(main))
    ordered_euro = validate_euro(list(euro))
    with closing(database_connection()) as connection, connection:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO saved_ticket(main_json, euro_json, label, created_at) VALUES (?, ?, ?, ?)",
            (
                json.dumps(ordered_main),
                json.dumps(ordered_euro),
                label[:80],
                datetime.now(UTC).isoformat(),
            ),
        )
        inserted = cursor.rowcount > 0
    line = f"{' '.join(map(str, ordered_main))} + {' '.join(map(str, ordered_euro))}"
    print(f"{'Saved' if inserted else 'Already saved'} locally: {line}")


def list_tickets() -> None:
    with closing(database_connection()) as connection:
        rows = connection.execute(
            "SELECT main_json, euro_json, label, created_at FROM saved_ticket ORDER BY id DESC"
        ).fetchall()
    if not rows:
        print("No Eurojackpot tickets saved locally.")
    for row in rows:
        main = " ".join(map(str, json.loads(row["main_json"])))
        euro = " ".join(map(str, json.loads(row["euro_json"])))
        label = f" — {row['label']}" if row["label"] else ""
        print(f"{main} + {euro}{label} ({row['created_at']})")


def load_draws() -> list[dict[str, object]]:
    with closing(database_connection()) as connection:
        rows = connection.execute(
            "SELECT draw_date, main_json, euro_json, source FROM draw ORDER BY id"
        ).fetchall()
    return [
        {
            "date": str(row["draw_date"]),
            "main": tuple(json.loads(row["main_json"])),
            "euro": tuple(json.loads(row["euro_json"])),
            "source": str(row["source"]),
        }
        for row in rows
    ]


def current_era_draws(draws: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        draw for draw in draws
        if not draw["date"] or str(draw["date"]) >= EURO_CURRENT_START
    ]


def draw_date_range(draws: list[dict[str, object]]) -> tuple[str, str]:
    dates = sorted(str(draw["date"]) for draw in draws if draw["date"])
    return (dates[0], dates[-1]) if dates else ("", "")


def draw_source_summary(draws: list[dict[str, object]]) -> str:
    sources = Counter(str(draw["source"] or "unknown") for draw in draws)
    return " | ".join(f"{source} ({count:,})" for source, count in sources.most_common())


def normalize_name(value: object) -> str:
    decomposed = unicodedata.normalize("NFD", str(value))
    ascii_text = "".join(character for character in decomposed if unicodedata.category(character) != "Mn")
    return re.sub(r"[^a-z0-9]", "", ascii_text.lower())


def parse_number_cell(value: object) -> int | None:
    text = str(value).strip()
    if not re.fullmatch(r"\d{1,2}", text):
        return None
    return int(text)


def parse_exact_numbers(values: list[object], count: int, maximum: int) -> tuple[int, ...] | None:
    numbers: list[int] = []
    for value in values:
        number = parse_number_cell(value)
        if number is None or not 1 <= number <= maximum or number in numbers:
            return None
        numbers.append(number)
    return tuple(sorted(numbers)) if len(numbers) == count else None


def collect_numbers(cells: list[object]) -> list[int]:
    numbers: list[int] = []
    for cell in cells:
        number = parse_number_cell(cell)
        if number is not None:
            numbers.append(number)
    return numbers


DATE_VALUE_PATTERN = re.compile(r"\d{1,4}[-./]\d{1,2}[-./]\d{1,4}")


def is_date_cell(value: object) -> bool:
    return bool(DATE_VALUE_PATTERN.search(str(value).strip()))


def normalize_draw_date(value: object) -> str:
    text = str(value).strip()
    if not text:
        return ""
    iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if iso:
        return text
    european = re.fullmatch(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", text)
    if european:
        return f"{european.group(3)}-{int(european.group(2)):02d}-{int(european.group(1)):02d}"
    return ""


def find_named_column(header: list[str], position: int, prefixes: tuple[str, ...]) -> int | None:
    candidates = {f"{prefix}{position}" for prefix in prefixes}
    return next((index for index, name in enumerate(header) if name in candidates), None)


def parse_csv_draws(text: str, header_mode: str = "auto") -> list[dict[str, object]]:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("The file is empty.")
    delimiter = max([",", ";", "\t"], key=lambda candidate: lines[0].count(candidate))
    rows = list(csv.reader(lines, delimiter=delimiter))
    header = [normalize_name(cell) for cell in rows[0]]
    main_indices = [
        find_named_column(header, position, ("n", "number", "zahl", "ziffer", "main", "lottozahl", "z"))
        for position in range(1, MAIN_PICK + 1)
    ]
    euro_indices = [
        find_named_column(
            header, position,
            ("e", "euro", "eurozahl", "additional", "additionalnumber", "zusatzzahl", "zusatz"),
        )
        for position in range(1, EURO_PICK + 1)
    ]
    date_index = next(
        (index for index, name in enumerate(header) if name in {"date", "datum", "drawdate", "ziehungstag", "day"}),
        None,
    )
    if header_mode not in {"auto", "yes", "no"}:
        raise ValueError("--header must be auto, yes, or no.")
    has_header = header_mode != "no" and all(index is not None for index in main_indices + euro_indices)
    start_row = 1 if header_mode == "yes" or has_header else 0
    records: list[dict[str, object]] = []
    for row in rows[start_row:]:
        if has_header:
            main = parse_exact_numbers(
                [row[index] if index is not None and index < len(row) else "" for index in main_indices],
                MAIN_PICK, MAIN_MAX,
            )
            euro = parse_exact_numbers(
                [row[index] if index is not None and index < len(row) else "" for index in euro_indices],
                EURO_PICK, EURO_MAX,
            )
            date = normalize_draw_date(row[date_index]) if date_index is not None and date_index < len(row) else ""
        else:
            date_index_row = next((index for index, cell in enumerate(row) if is_date_cell(cell)), None)
            date = normalize_draw_date(row[date_index_row]) if date_index_row is not None else ""
            cells = row[date_index_row + 1:] if date_index_row is not None else row
            numbers = collect_numbers(cells)
            if len(numbers) >= 7:
                numbers = numbers[-7:]
                main = parse_exact_numbers(numbers[:MAIN_PICK], MAIN_PICK, MAIN_MAX)
                euro = parse_exact_numbers(numbers[MAIN_PICK:], EURO_PICK, EURO_MAX)
            else:
                main = euro = None
        if main is not None and euro is not None:
            records.append({"date": date, "main": main, "euro": euro})
    if not records:
        raise ValueError("No valid Eurojackpot rows found in the CSV.")
    return records


def parse_json_draws(text: str) -> list[dict[str, object]]:
    data = json.loads(text)
    if isinstance(data, dict):
        for key in ("draws", "rows", "results", "data", "tickets"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            raise TypeError("JSON must contain a draws/rows/results array.")
    if not isinstance(data, list):
        raise TypeError("JSON must contain an array of draw rows.")
    records: list[dict[str, object]] = []
    for item in data:
        main: tuple[int, ...] | None = None
        euro: tuple[int, ...] | None = None
        date = ""
        if isinstance(item, (list, tuple)):
            values = list(item)
            if len(values) == 2 and isinstance(values[0], (list, tuple)) and isinstance(values[1], (list, tuple)):
                main = parse_exact_numbers(list(values[0]), MAIN_PICK, MAIN_MAX)
                euro = parse_exact_numbers(list(values[1]), EURO_PICK, EURO_MAX)
            else:
                if values and is_date_cell(values[0]):
                    date = normalize_draw_date(values[0])
                    values = values[1:]
                numbers = collect_numbers(values)
                if len(numbers) >= 7:
                    numbers = numbers[-7:]
                    main = parse_exact_numbers(numbers[:MAIN_PICK], MAIN_PICK, MAIN_MAX)
                    euro = parse_exact_numbers(numbers[MAIN_PICK:], EURO_PICK, EURO_MAX)
        elif isinstance(item, dict):
            for key in ("main", "numbers", "n", "zahlen", "drawn"):
                value = item.get(key)
                if isinstance(value, (list, tuple)):
                    main = parse_exact_numbers(list(value), MAIN_PICK, MAIN_MAX)
                    break
            for key in ("euro", "e", "additional", "additionalNumbers", "eurozahlen"):
                value = item.get(key)
                if isinstance(value, (list, tuple)):
                    euro = parse_exact_numbers(list(value), EURO_PICK, EURO_MAX)
                    break
            if main is None:
                main = parse_exact_numbers(
                    [item.get(f"n{position}") for position in range(1, MAIN_PICK + 1)],
                    MAIN_PICK, MAIN_MAX,
                )
            if euro is None:
                euro = parse_exact_numbers(
                    [item.get(f"e{position}") for position in range(1, EURO_PICK + 1)],
                    EURO_PICK, EURO_MAX,
                )
            date = normalize_draw_date(
                item.get("d") or item.get("date") or item.get("datum") or item.get("draw_date") or ""
            )
        if main is not None and euro is not None:
            records.append({"date": date, "main": main, "euro": euro})
    if not records:
        raise ValueError("No valid Eurojackpot rows found in the JSON.")
    return records


def parse_draw_text(text: str, header_mode: str = "auto") -> list[dict[str, object]]:
    stripped = text.strip()
    if not stripped:
        raise ValueError("The file is empty.")
    if stripped[0] in "[{":
        return parse_json_draws(stripped)
    return parse_csv_draws(stripped, header_mode=header_mode)


def read_draw_file(path: str | Path, header_mode: str = "auto") -> list[dict[str, object]]:
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise ValueError(f"File not found: {file_path}")
    return parse_draw_text(file_path.read_text(encoding="utf-8-sig"), header_mode=header_mode)


def fetch_json_url(url: str, timeout: float = 8.0) -> object:
    request = Request(
        url,
        headers={
            "User-Agent": "LottoAnalyzer/1.0 (educational; Python stdlib)",
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_draw_years(timeout: float = 8.0) -> list[int]:
    payload = fetch_json_url(DRAW_YEARS_URL, timeout)
    if not isinstance(payload, dict) or not isinstance(payload.get("years"), list):
        raise TypeError("Eurojackpot draw-years response is invalid.")
    years = [strict_int(year, "year") for year in payload["years"]]
    return sorted(years)


def fetch_draw_keys(year: int, timeout: float = 8.0) -> list[dict[str, object]]:
    payload = fetch_json_url(DRAW_KEYS_URL.format(year=year), timeout)
    if not isinstance(payload, list):
        raise TypeError("Eurojackpot draw-keys response is invalid.")
    return [entry for entry in payload if isinstance(entry, dict) and entry.get("key")]


def fetch_official_draw(key: str, event_date: str = "", timeout: float = 8.0) -> dict[str, object]:
    payload = fetch_json_url(DRAW_NUMBERS_URL.format(key=key), timeout)
    if not isinstance(payload, dict):
        raise TypeError("Eurojackpot historic-number response is invalid.")
    main = parse_exact_numbers(payload.get("numbersInNaturalOrder"), MAIN_PICK, MAIN_MAX)
    euro = parse_exact_numbers(payload.get("additionalNumbersInNaturalOrder"), EURO_PICK, EURO_MAX)
    if main is None or euro is None:
        raise ValueError(f"Invalid historic draw {key}.")
    draw_date = timestamp_to_date(payload.get("drawDate")) or event_date
    return {"date": draw_date, "main": main, "euro": euro}


def sync_draws(years: int = 0, max_draws: int = 0, timeout: float = 8.0) -> None:
    if years < 0:
        raise ValueError("--years must be zero or positive.")
    if max_draws < 0:
        raise ValueError("--max-draws must be zero or positive.")
    available_years = fetch_draw_years(timeout)
    selected_years = available_years[-years:] if years > 0 else available_years
    existing_dates = {str(draw["date"]) for draw in load_draws() if draw["date"]}
    records: list[dict[str, object]] = []
    skipped = 0
    for year in selected_years:
        for entry in fetch_draw_keys(year, timeout):
            key = str(entry["key"])
            event_date = timestamp_to_date(entry.get("eventDate"))
            if event_date and event_date in existing_dates:
                continue
            try:
                record = fetch_official_draw(key, event_date, timeout)
            except (OSError, ValueError, TypeError) as error:
                skipped += 1
                print(f"Warning: could not import official draw {key}: {error}", file=sys.stderr)
                continue
            if record["date"] and record["date"] in existing_dates:
                continue
            records.append(record)
            if record["date"]:
                existing_dates.add(str(record["date"]))
            if max_draws and len(records) >= max_draws:
                break
        if max_draws and len(records) >= max_draws:
            break
    if records:
        with closing(database_connection()) as connection, connection:
            connection.executemany(
                "INSERT INTO draw(draw_date, main_json, euro_json, source) VALUES (?, ?, ?, ?)",
                [
                    (str(record["date"]), json.dumps(record["main"]), json.dumps(record["euro"]), OFFICIAL_DRAW_SOURCE)
                    for record in records
                ],
            )
    first_date, last_date = draw_date_range(records)
    range_text = f" ({first_date} to {last_date})" if first_date else ""
    print(f"Imported {len(records):,} official Eurojackpot rows{range_text}; skipped {skipped:,}.")


def load_bundled_draws() -> list[dict[str, object]]:
    if not DEFAULT_DRAWS.is_file():
        return []
    records = read_draw_file(DEFAULT_DRAWS)
    for record in records:
        record["source"] = "Bundled official LOTTO Hessen historic draws"
    return records


def import_draws(path: str | Path, append: bool = False, source: str = "", header_mode: str = "auto") -> None:
    records = read_draw_file(path, header_mode=header_mode)
    source_note = source.strip() or str(path)
    with closing(database_connection()) as connection, connection:
        if not append:
            connection.execute("DELETE FROM draw")
        connection.executemany(
            "INSERT INTO draw(draw_date, main_json, euro_json, source) VALUES (?, ?, ?, ?)",
            [
                (str(record["date"]), json.dumps(record["main"]), json.dumps(record["euro"]), source_note)
                for record in records
            ],
        )
    first_date, last_date = draw_date_range(records)
    range_text = f" ({first_date} to {last_date})" if first_date else ""
    print(f"Imported {len(records):,} Eurojackpot draw rows from {source_note}{range_text}.")


def minimum_snapshot_draw_counts() -> tuple[int, int]:
    with closing(database_connection()) as connection:
        stored_main, stored_euro = stored_draw_counts(connection)
    try:
        bundled = load_bundled_stats()
        bundled_main = int(bundled["main_draw_count"])
        bundled_euro = int(bundled["euro_draw_count"])
    except (ValueError, TypeError, OSError):
        bundled_main = bundled_euro = 0
    return max(stored_main, bundled_main), max(stored_euro, bundled_euro)


def refresh_snapshot() -> None:
    stats = fetch_live_stats()
    minimum_main, minimum_euro = minimum_snapshot_draw_counts()
    if int(stats["main_draw_count"]) < minimum_main or int(stats["euro_draw_count"]) < minimum_euro:
        raise ValueError(
            "Eurojackpot statistics returned fewer draws than the stored/bundled data "
            f"(main {int(stats['main_draw_count']):,}/{minimum_main:,}, "
            f"Euro {int(stats['euro_draw_count']):,}/{minimum_euro:,})."
        )
    store_snapshot(stats)
    if stats.get("cross_check") == "mismatch":
        print(
            "Warning: WestLotto and LOTTO Hessen main-number counts disagree; "
            "using LOTTO Hessen.",
            file=sys.stderr,
        )
    print(
        f"Eurojackpot snapshot refreshed: main {int(stats['main_draw_count']):,} draws, "
        f"Euro {int(stats['euro_draw_count']):,} draws. {stats['source_note']}"
    )


def initialize_database(scrape: bool = True, seed_draws: bool = True) -> None:
    with closing(database_connection()) as connection, connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS frequency_snapshot (
                kind TEXT NOT NULL CHECK(kind IN ('main', 'euro')),
                number INTEGER NOT NULL,
                drawn_count INTEGER NOT NULL CHECK(drawn_count >= 0),
                source_note TEXT NOT NULL,
                draw_count INTEGER NOT NULL CHECK(draw_count > 0),
                start_date TEXT NOT NULL DEFAULT '',
                end_date TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (kind, number)
            );
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS draw (
                id INTEGER PRIMARY KEY,
                draw_date TEXT NOT NULL DEFAULT '',
                main_json TEXT NOT NULL,
                euro_json TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS saved_ticket (
                id INTEGER PRIMARY KEY,
                main_json TEXT NOT NULL,
                euro_json TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(main_json, euro_json)
            );
            CREATE TABLE IF NOT EXISTS player_ticket (
                id INTEGER PRIMARY KEY,
                main_json TEXT NOT NULL,
                euro_json TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
        """)
        if not snapshot_is_valid(connection):
            write_snapshot(connection, load_bundled_stats())
        if seed_draws and connection.execute("SELECT COUNT(*) FROM draw").fetchone()[0] == 0:
            bundled_draws = load_bundled_draws()
            if bundled_draws:
                connection.executemany(
                    "INSERT INTO draw(draw_date, main_json, euro_json, source) VALUES (?, ?, ?, ?)",
                    [
                        (str(record["date"]), json.dumps(record["main"]), json.dumps(record["euro"]), str(record["source"]))
                        for record in bundled_draws
                    ],
                )
    if scrape:
        try:
            stats = fetch_live_stats()
            minimum_main, minimum_euro = minimum_snapshot_draw_counts()
            if int(stats["main_draw_count"]) < minimum_main or int(stats["euro_draw_count"]) < minimum_euro:
                raise ValueError(
                    "Eurojackpot statistics returned fewer draws than the stored/bundled data "
                    f"(main {int(stats['main_draw_count']):,}/{minimum_main:,}, "
                    f"Euro {int(stats['euro_draw_count']):,}/{minimum_euro:,})"
                )
        except (OSError, ValueError, TypeError, sqlite3.Error) as error:
            print(
                f"Warning: Eurojackpot statistics refresh failed ({error}); using the stored snapshot.",
                file=sys.stderr,
            )
        else:
            if stats.get("cross_check") == "mismatch":
                print(
                    "Warning: WestLotto and LOTTO Hessen main-number counts disagree; "
                    "using LOTTO Hessen.",
                    file=sys.stderr,
                )
            try:
                store_snapshot(stats)
            except (ValueError, sqlite3.Error) as error:
                print(
                    f"Warning: could not store Eurojackpot statistics ({error}); using the stored snapshot.",
                    file=sys.stderr,
                )


def reseed_snapshot() -> None:
    stats = load_bundled_stats()
    store_snapshot(stats)
    print(
        f"Eurojackpot snapshot reseeded from bundled data: main {int(stats['main_draw_count']):,} draws, "
        f"Euro {int(stats['euro_draw_count']):,} draws."
    )


def build_sum_counts(number_count: int, pick_count: int) -> list[int]:
    ways = [[0] * (number_count * pick_count + 1) for _ in range(pick_count + 1)]
    ways[0][0] = 1
    for number in range(1, number_count + 1):
        for picked in range(pick_count, 0, -1):
            for ticket_sum in range(number_count * pick_count, number - 1, -1):
                ways[picked][ticket_sum] += ways[picked - 1][ticket_sum - number]
    return ways[pick_count]


MAIN_SUM_COUNTS = build_sum_counts(MAIN_MAX, MAIN_PICK)
EURO_SUM_COUNTS = build_sum_counts(EURO_MAX, EURO_PICK)


def distribution_stats(distribution: list[int]) -> tuple[float, float]:
    total = sum(distribution)
    mean = sum(value * count for value, count in enumerate(distribution)) / total
    variance = sum((value - mean) ** 2 * count for value, count in enumerate(distribution)) / total
    return mean, math.sqrt(variance)


MAIN_SUM_MEAN, MAIN_SUM_SIGMA = distribution_stats(MAIN_SUM_COUNTS)
EURO_SUM_MEAN, EURO_SUM_SIGMA = distribution_stats(EURO_SUM_COUNTS)


def odd_even_distribution(number_count: int, pick_count: int) -> list[int]:
    odd_numbers = number_count // 2
    even_numbers = number_count - odd_numbers
    return [
        math.comb(odd_numbers, odd) * math.comb(even_numbers, pick_count - odd)
        for odd in range(pick_count + 1)
    ]


def low_high_distribution(number_count: int, pick_count: int, low_max: int) -> list[int]:
    high_numbers = number_count - low_max
    return [
        math.comb(low_max, low) * math.comb(high_numbers, pick_count - low)
        for low in range(pick_count + 1)
    ]


def birthday_distribution(pick_count: int) -> list[int]:
    return [
        math.comb(BIRTHDAY_MAX, birthday) * math.comb(MAIN_MAX - BIRTHDAY_MAX, pick_count - birthday)
        for birthday in range(pick_count + 1)
    ]


def adjacency_distribution(number_count: int, pick_count: int) -> list[int]:
    state = [[[0, 0] for _ in range(pick_count)] for _ in range(pick_count + 1)]
    state[0][0][0] = 1
    for _ in range(1, number_count + 1):
        next_state = [[[0, 0] for _ in range(pick_count)] for _ in range(pick_count + 1)]
        for chosen in range(pick_count + 1):
            for adjacent in range(pick_count):
                for previous in (0, 1):
                    ways = state[chosen][adjacent][previous]
                    if not ways:
                        continue
                    next_state[chosen][adjacent][0] += ways
                    if chosen < pick_count:
                        next_state[chosen + 1][adjacent + previous][1] += ways
        state = next_state
    return [state[pick_count][adjacent][0] + state[pick_count][adjacent][1] for adjacent in range(pick_count)]


def range_distribution(number_count: int, pick_count: int) -> list[int]:
    counts = [0] * number_count
    for span in range(pick_count - 1, number_count):
        counts[span] = (number_count - span) * math.comb(span - 1, pick_count - 2)
    return counts


MAIN_ODD_EVEN = odd_even_distribution(MAIN_MAX, MAIN_PICK)
MAIN_LOW_HIGH = low_high_distribution(MAIN_MAX, MAIN_PICK, LOW_MAX)
MAIN_BIRTHDAY = birthday_distribution(MAIN_PICK)
MAIN_ADJACENCY = adjacency_distribution(MAIN_MAX, MAIN_PICK)
MAIN_RANGE = range_distribution(MAIN_MAX, MAIN_PICK)
EURO_ODD_EVEN = odd_even_distribution(EURO_MAX, EURO_PICK)
MAX_MAIN_SUM_COUNT = max(MAIN_SUM_COUNTS)
MAX_MAIN_ODD_EVEN = max(MAIN_ODD_EVEN)
MAX_MAIN_LOW_HIGH = max(MAIN_LOW_HIGH)
MAX_MAIN_ADJACENCY = max(MAIN_ADJACENCY)
MAX_MAIN_RANGE = max(MAIN_RANGE)


def frequency_statistics(
    counts: list[int], draw_count: int, pick_count: int, number_count: int
) -> dict[str, object]:
    probability = pick_count / number_count
    expected = draw_count * probability
    sigma = math.sqrt(draw_count * probability * (1 - probability))
    pair_probability = (pick_count / number_count) * ((pick_count - 1) / (number_count - 1))
    eigenvalue = probability - pair_probability
    adjusted = sum((count - expected) ** 2 for count in counts) / (draw_count * eigenvalue)
    z_scores = [(count - expected) / sigma for count in counts]
    return {
        "counts": counts,
        "draw_count": draw_count,
        "expected": expected,
        "sigma": sigma,
        "z": z_scores,
        "adjusted_chi_square": adjusted,
        "degrees_of_freedom": number_count - 1,
        "p_value": chi2_sf(adjusted, number_count - 1),
        "pearson_chi_square": sum((count - expected) ** 2 / expected for count in counts),
        "probability": probability,
    }


def validate_main(numbers: list[int]) -> tuple[int, ...]:
    if len(numbers) != MAIN_PICK:
        raise ValueError("Provide exactly five main numbers.")
    if any(not 1 <= number <= MAIN_MAX for number in numbers):
        raise ValueError(f"Each main number must be 1 through {MAIN_MAX}.")
    ordered = tuple(sorted(numbers))
    if len(set(ordered)) != MAIN_PICK:
        raise ValueError("Main numbers must be unique.")
    return ordered


def validate_euro(numbers: list[int]) -> tuple[int, ...]:
    if len(numbers) != EURO_PICK:
        raise ValueError("Provide exactly two Euro numbers.")
    if any(not 1 <= number <= EURO_MAX for number in numbers):
        raise ValueError(f"Each Euro number must be 1 through {EURO_MAX}.")
    ordered = tuple(sorted(numbers))
    if len(set(ordered)) != EURO_PICK:
        raise ValueError("Euro numbers must be unique.")
    return ordered


def adjacent_count(numbers: tuple[int, ...]) -> int:
    return sum(1 for earlier, later in pairwise(numbers) if later == earlier + 1)


def longest_run(numbers: tuple[int, ...]) -> int:
    longest = current = 1
    for earlier, later in pairwise(numbers):
        current = current + 1 if later == earlier + 1 else 1
        longest = max(longest, current)
    return longest


def analyze_ticket(main: tuple[int, ...], euro: tuple[int, ...]) -> dict[str, object]:
    ordered_main = tuple(sorted(main))
    ordered_euro = tuple(sorted(euro))
    main_sum = sum(ordered_main)
    main_odd = sum(number % 2 for number in ordered_main)
    main_low = sum(number <= LOW_MAX for number in ordered_main)
    birthday = sum(number <= BIRTHDAY_MAX for number in ordered_main)
    span = ordered_main[-1] - ordered_main[0]
    euro_sum = sum(ordered_euro)
    euro_odd = sum(number % 2 for number in ordered_euro)
    return {
        "main": ordered_main,
        "euro": ordered_euro,
        "main_sum": main_sum,
        "main_sum_count": MAIN_SUM_COUNTS[main_sum],
        "main_odd_count": main_odd,
        "main_even_count": MAIN_PICK - main_odd,
        "main_parity_count": MAIN_ODD_EVEN[main_odd],
        "main_low_count": main_low,
        "main_low_high_count": MAIN_LOW_HIGH[main_low],
        "main_birthday_count": birthday,
        "main_birthday_category_count": MAIN_BIRTHDAY[birthday],
        "main_adjacent_count": adjacent_count(ordered_main),
        "main_longest_run": longest_run(ordered_main),
        "main_range": span,
        "main_range_count": MAIN_RANGE[span],
        "euro_sum": euro_sum,
        "euro_sum_count": EURO_SUM_COUNTS[euro_sum],
        "euro_odd_count": euro_odd,
        "euro_even_count": EURO_PICK - euro_odd,
        "euro_parity_count": EURO_ODD_EVEN[euro_odd],
    }


def assess_ticket(main: tuple[int, ...], euro: tuple[int, ...]) -> list[tuple[str, str]]:
    ordered = tuple(sorted(main))
    birthday = sum(number <= BIRTHDAY_MAX for number in ordered)
    adjacency = adjacent_count(ordered)
    run = longest_run(ordered)
    flags: list[tuple[str, str]] = []
    if birthday == MAIN_PICK:
        flags.append(("warn", "All five main numbers are in the birthday range (1–31)."))
    elif birthday >= 4:
        flags.append(("warn", f"{birthday} of 5 main numbers are in the birthday range (1–31)."))
    if run >= 3:
        flags.append(("warn", f"Main numbers contain a consecutive run of {run}."))
    if adjacency >= 3:
        flags.append(("warn", f"Main numbers contain {adjacency} adjacent-number pairs."))
    if all(number % 5 == 0 for number in ordered):
        flags.append(("warn", "All main numbers are multiples of five."))
    if all(number % 2 == 0 for number in ordered):
        flags.append(("warn", "All five main numbers are even."))
    if all(number % 2 == 1 for number in ordered):
        flags.append(("warn", "All five main numbers are odd."))
    if len({number // 10 for number in ordered}) == 1:
        flags.append(("warn", "All five main numbers fall in the same decade."))
    if not flags:
        flags.append(("good", "No obvious birthday-only or simple sequence pattern detected."))
    return flags


def score_ticket(
    main: tuple[int, ...], euro: tuple[int, ...], profile: str = "anti-sharing"
) -> tuple[float, dict[str, float]]:
    """Score the main-number pattern 0-100; not a win probability."""
    if profile not in {"typical", "anti-sharing"}:
        raise ValueError("Profile must be 'typical' or 'anti-sharing'.")
    ordered = validate_main(list(main))
    validate_euro(list(euro))
    main_sum = sum(ordered)
    odd_count = sum(number % 2 for number in ordered)
    low_count = sum(number <= LOW_MAX for number in ordered)
    high_birthday_count = sum(number > BIRTHDAY_MAX for number in ordered)
    span = ordered[-1] - ordered[0]
    if profile == "typical":
        components = {
            "sum": MAIN_SUM_COUNTS[main_sum] / MAX_MAIN_SUM_COUNT,
            "parity": MAIN_ODD_EVEN[odd_count] / MAX_MAIN_ODD_EVEN,
            "low_high": MAIN_LOW_HIGH[low_count] / MAX_MAIN_LOW_HIGH,
            "range": MAIN_RANGE[span] / MAX_MAIN_RANGE,
            "adjacency": MAIN_ADJACENCY[adjacent_count(ordered)] / MAX_MAIN_ADJACENCY,
            "birthday": min(1.0, high_birthday_count / 2),
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
            "parity_rarity": 1.0 - MAIN_ODD_EVEN[odd_count] / MAX_MAIN_ODD_EVEN,
            "birthday": min(1.0, high_birthday_count / 2),
            "adjacency": MAIN_ADJACENCY[adjacent_count(ordered)] / MAX_MAIN_ADJACENCY,
            "range_spread": min(1.0, max(0.0, (span - 4) / 45)),
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


def format_odds(denominator: float) -> str:
    if math.isclose(denominator, round(denominator), rel_tol=1e-12, abs_tol=1e-9):
        return f"1 in {round(denominator):,}"
    return f"1 in {denominator:,.2f}"


def one_in(total: int, category_count: int) -> str:
    return format_odds(total / category_count)


def short_flags(main: tuple[int, ...], euro: tuple[int, ...]) -> str:
    flags = assess_ticket(main, euro)
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


def any_prize_probability() -> float:
    return sum(EURO_PRIZE_COUNTS) / EURO_PRIZE_FULL_OUTCOMES


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
    other = [0.0] * (len(EURO_PRIZE_CLASSES) - 1) if other_prizes is None else list(other_prizes)
    if len(other) != len(EURO_PRIZE_CLASSES) - 1:
        raise ValueError(f"Provide exactly {len(EURO_PRIZE_CLASSES) - 1} lower-class prize amounts.")
    if not math.isfinite(jackpot) or jackpot < 0:
        raise ValueError("Jackpot must be a non-negative finite number.")
    if not math.isfinite(stake) or stake <= 0:
        raise ValueError("Stake must be a positive finite number.")
    if any(not math.isfinite(value) or value < 0 for value in other):
        raise ValueError("Prize amounts must be non-negative finite numbers.")
    amounts = [jackpot, *other]
    contributions = [
        amount * count / EURO_PRIZE_FULL_OUTCOMES
        for amount, count in zip(amounts, EURO_PRIZE_COUNTS)
    ]
    expected = sum(contributions)
    lower_expected = sum(contributions[1:])
    jackpot_odds = EURO_PRIZE_CLASSES[0][2]
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
    print(f"\nEurojackpot 5/50 + 2/12 official prize classes\n{'=' * 82}")
    print("Class | Match | Official odds | Exact odds | Payout share")
    for (class_no, match, odds, share), count in zip(EURO_PRIZE_CLASSES, EURO_PRIZE_COUNTS):
        exact = format_odds(EURO_PRIZE_FULL_OUTCOMES / count)
        print(f"{class_no:5} | {match:9} | 1 in {odds:>13,} | {exact:>15} | {share}")
    print(f"\nAny prize class: {format_odds(1 / any_prize_probability())}")
    print(f"Source: {EURO_PRIZE_SOURCE}")
    print("Official odds are published rounded values; exact odds use the official game rules.")
    print("Payout shares exclude the 9.00% Boosterfonds. Actual prizes are pari-mutuel.")


def print_ev(jackpot: float, other_prizes_text: str, stake: float) -> None:
    other = parse_prize_amounts(other_prizes_text, len(EURO_PRIZE_CLASSES) - 1)
    result = calculate_ev(jackpot, other, stake)
    amounts = result["amounts"]  # type: ignore[index]
    contributions = result["contributions"]  # type: ignore[index]
    print(f"\nEurojackpot expected-value estimate\n{'=' * 82}")
    print("Class | Match | Exact odds | Prize amount | EV contribution")
    for (class_no, match, _odds, _share), count, amount, contribution in zip(
        EURO_PRIZE_CLASSES, EURO_PRIZE_COUNTS, amounts, contributions  # type: ignore[arg-type]
    ):
        print(f"{class_no:5} | {match:9} | {format_odds(EURO_PRIZE_FULL_OUTCOMES / count):>15} | {amount:12,.2f} | {contribution:12.6f}")
    print(f"\nStake per ticket: {stake:,.2f}")
    print(f"Expected return: {float(result['expected_value']):,.6f} ({float(result['return_pct']):.2%} of stake)")
    print(f"Net expected value: {float(result['net']):+,.6f}")
    print(f"Break-even jackpot with supplied lower prizes: {float(result['break_even_jackpot']):,.2f}")
    print(f"Break-even jackpot if only class 1 paid: {float(result['jackpot_only_break_even']):,.2f}")
    print("Only the prize amounts you supplied are counted. Pari-mutuel amounts and sharing vary.")


def random_ticket(rng: random.Random | None = None) -> tuple[tuple[int, ...], tuple[int, ...]]:
    generator = rng if rng is not None else random.SystemRandom()
    main = tuple(sorted(generator.sample(range(1, MAIN_MAX + 1), MAIN_PICK)))
    euro = tuple(sorted(generator.sample(range(1, EURO_MAX + 1), EURO_PICK)))
    return main, euro


def generate_ranked_tickets(
    runs: int = 100,
    rng: random.Random | None = None,
    profile: str = "anti-sharing",
    popularity: dict[str, object] | None = None,
) -> list[tuple[float, tuple[int, ...], tuple[int, ...], dict[str, float]]]:
    if runs <= 0:
        raise ValueError("The number of runs must be a positive integer.")
    results: list[tuple[float, tuple[int, ...], tuple[int, ...], dict[str, float]]] = []
    for _ in range(runs):
        main, euro = random_ticket(rng)
        if profile == "empirical":
            if popularity is None:
                raise ValueError("Empirical scoring requires player-selection data.")
            score, components = empirical_sharing_score(main, euro, popularity)
        else:
            score, components = score_ticket(main, euro, profile=profile)
        results.append((score, main, euro, components))
    results.sort(key=lambda item: (-item[0], item[1], item[2]))
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
    print(f"\nEurojackpot 5/50 + 2/12 — {label} profile — top {len(displayed)} of {runs:,} random tickets\n{'=' * 108}")
    print("Score is a main-number pattern/sharing heuristic, not a draw probability.")
    print(f"Exact main odds: 1 in {MAIN_COMBINATIONS:,} | full 5+2 odds: 1 in {FULL_COMBINATIONS:,}.")
    if profile == "empirical":
        print("Score is based on inverse measured main/Euro number popularity from the imported player sample.")
        print("Main popularity weight 70%, Euro 30%; exact-combination duplicate penalty ×0.5.")
    elif profile == "anti-sharing":
        print("Weights: parity rarity 35%, birthday spread 25%, no adjacency 20%, range spread 10%, decade spread 10%.")
        print("Anti-sharing is an assumption-based heuristic; no player-selection data is used.")
        print("Pattern flags remain descriptive; rare parity is rewarded by this profile.")
    else:
        print("Weights: sum 20%, parity 15%, low/high 15%, range 10%, adjacency 20%, birthday spread 20%.")
    print("\nRank | Main numbers        | Euro   | Score | Sum | Odd/Even | Low/High | ≤31 | Adj | Run | Flags")
    print("-" * 108)
    for rank, (score, main, euro, _components) in enumerate(displayed, 1):
        analysis = analyze_ticket(main, euro)
        print(
            f"{rank:4} | {' '.join(f'{number:2}' for number in main):19} | "
            f"{' '.join(f'{number:2}' for number in euro):6} | {score:5.1f}% | "
            f"{int(analysis['main_sum']):4} | "
            f"{int(analysis['main_odd_count'])}/{int(analysis['main_even_count'])}      | "
            f"{int(analysis['main_low_count'])}/{MAIN_PICK - int(analysis['main_low_count'])}      | "
            f"{int(analysis['main_birthday_count']):3} | "
            f"{int(analysis['main_adjacent_count']):3} | {int(analysis['main_longest_run']):3} | "
            f"{short_flags(main, euro)}"
        )
    scores = [score for score, _main, _euro, _components in results]
    print(
        f"\nHighest: {max(scores):.1f}% | Lowest: {min(scores):.1f}% | "
        f"Mean: {sum(scores) / len(scores):.1f}% | Displayed mean: "
        f"{sum(score for score, _main, _euro, _components in displayed) / len(displayed):.1f}%"
    )
    if profile == "empirical":
        print("Less-selected numbers and non-duplicated combinations score higher in this sample.")
    elif profile == "anti-sharing":
        print("Rare parity, numbers above 31, no adjacency, wide range, and decade spread score higher.")
    else:
        print("Balanced 2/3 or 3/2 parity, sums near 127.5, no adjacency, and numbers above 31 score higher.")
    print("The ranking is not a forecast and cannot improve the chance of winning.")


def print_ticket_analysis(main: tuple[int, ...], euro: tuple[int, ...]) -> None:
    analysis = analyze_ticket(main, euro)
    snapshot = load_snapshot()
    main_draw_count = int(snapshot["main_draw_count"])
    euro_draw_count = int(snapshot["euro_draw_count"])
    main_counts = snapshot["main"]
    euro_counts = snapshot["euro"]
    main_stats = frequency_statistics(main_counts, main_draw_count, MAIN_PICK, MAIN_MAX)  # type: ignore[arg-type]
    euro_stats = frequency_statistics(euro_counts, euro_draw_count, EURO_PICK, EURO_MAX)  # type: ignore[arg-type]
    main_expected, main_sigma = float(main_stats["expected"]), float(main_stats["sigma"])
    euro_expected, euro_sigma = float(euro_stats["expected"]), float(euro_stats["sigma"])
    print(
        f"\nEurojackpot ticket: main {' '.join(f'{number:2}' for number in main)} | "
        f"Euro {' '.join(f'{number:2}' for number in euro)}\n{'=' * 72}"
    )
    print(f"Exact main odds       1 in {MAIN_COMBINATIONS:,}")
    print(f"Full 5+2 jackpot      1 in {FULL_COMBINATIONS:,}")
    typical_score, _components = score_ticket(main, euro, "typical")
    anti_score, _components = score_ticket(main, euro, "anti-sharing")
    print(
        f"Pattern scores        typical: {typical_score:.1f}/100 | "
        f"anti-sharing: {anti_score:.1f}/100 (main-number heuristics)"
    )
    print("Anti-sharing rewards rare parity by assumption; pattern flags remain descriptive.")
    print(
        f"Main sum              {analysis['main_sum']}  | "
        f"{analysis['main_sum_count']:,} lines | {one_in(MAIN_COMBINATIONS, int(analysis['main_sum_count']))}"
    )
    print(
        f"Main odd / even       {analysis['main_odd_count']} / {analysis['main_even_count']}  | "
        f"{int(analysis['main_parity_count']) / MAIN_COMBINATIONS:.2%} of all lines"
    )
    print(
        f"Main low / high       {analysis['main_low_count']} / {MAIN_PICK - int(analysis['main_low_count'])}  | "
        f"low = 1–{LOW_MAX}  | {int(analysis['main_low_high_count']) / MAIN_COMBINATIONS:.2%} of all lines"
    )
    print(
        f"Main birth range      {analysis['main_birthday_count']} of 5 ≤ 31  | "
        f"{int(analysis['main_birthday_category_count']) / MAIN_COMBINATIONS:.2%} of all lines"
    )
    print(
        f"Main adjacency        {analysis['main_adjacent_count']} adjacent pairs | "
        f"longest run: {analysis['main_longest_run']}"
    )
    print(
        f"Main range            {analysis['main_range']}  | "
        f"{int(analysis['main_range_count']) / MAIN_COMBINATIONS:.2%} of all lines"
    )
    print(
        f"Euro sum              {analysis['euro_sum']}  | "
        f"{analysis['euro_sum_count']:,} pairs | {one_in(EURO_COMBINATIONS, int(analysis['euro_sum_count']))}"
    )
    print(
        f"Euro odd / even       {analysis['euro_odd_count']} / {analysis['euro_even_count']}  | "
        f"{int(analysis['euro_parity_count']) / EURO_COMBINATIONS:.2%} of all Euro pairs"
    )
    print(
        f"\nHistorical frequency (main: {main_draw_count:,} draws; Euro: {euro_draw_count:,} draws; "
        f"descriptive only)"
    )
    for number in main:
        count = int(main_counts[number - 1])
        print(f"  main {number:2}: {count:>4} appearances ({(count - main_expected) / main_sigma:+.2f}σ)")
    for number in euro:
        count = int(euro_counts[number - 1])
        print(f"  euro {number:2}: {count:>4} appearances ({(count - euro_expected) / euro_sigma:+.2f}σ)")
    print("\nSharing-risk flags (qualitative, not a draw probability)")
    for level, message in assess_ticket(main, euro):
        print(f"  [{level.upper()}] {message}")
    print("Every exact 5+2 ticket still has the same probability. Past frequencies do not predict draws.")


def print_status() -> None:
    snapshot = load_snapshot()
    draws = load_draws()
    with closing(database_connection()) as connection:
        cross_check = get_metadata(connection, "last_cross_check") or "not recorded"
        cross_check_at = get_metadata(connection, "last_cross_check_at") or "unknown"
        saved_count = connection.execute("SELECT COUNT(*) FROM saved_ticket").fetchone()[0]
        player_count = connection.execute("SELECT COUNT(*) FROM player_ticket").fetchone()[0]
    print(f"\nEurojackpot data status\n{'=' * 72}")
    print(f"Snapshot source: {snapshot['source_note']}")
    print(
        f"Main snapshot: {int(snapshot['main_draw_count']):,} draws "
        f"({snapshot['main_start_date']} to {snapshot['main_end_date']})"
    )
    print(
        f"Euro snapshot: {int(snapshot['euro_draw_count']):,} draws "
        f"({snapshot['euro_start_date']} to {snapshot['euro_end_date']})"
    )
    print(f"Snapshot updated: {snapshot['updated_at']}")
    print(f"Source cross-check: {cross_check} ({cross_check_at})")
    if draws:
        first_date, last_date = draw_date_range(draws)
        print(f"Imported row-level draws: {len(draws):,} ({first_date or 'unknown'} to {last_date or 'unknown'})")
        print(f"Imported sources: {draw_source_summary(draws)}")
        print(f"Euro rows in current 2/12 era: {len(current_era_draws(draws)):,}")
    else:
        print("Imported row-level draws: none")
    print(f"Saved tickets: {saved_count:,} | player-selection lines: {player_count:,}")


def print_report() -> None:
    snapshot = load_snapshot()
    main_draw_count = int(snapshot["main_draw_count"])
    euro_draw_count = int(snapshot["euro_draw_count"])
    expected_main = main_draw_count * MAIN_PICK / MAIN_MAX
    expected_euro = euro_draw_count * EURO_PICK / EURO_MAX
    main_sigma = math.sqrt(main_draw_count * (MAIN_PICK / MAIN_MAX) * (1 - MAIN_PICK / MAIN_MAX))
    euro_sigma = math.sqrt(euro_draw_count * (EURO_PICK / EURO_MAX) * (1 - EURO_PICK / EURO_MAX))
    print(f"\nEurojackpot 5/50 + 2/12 — probability report\n{'=' * 72}")
    print(f"Data: {snapshot['source_note']}")
    print(
        f"Main history: {main_draw_count:,} draws ({snapshot['main_start_date']} to {snapshot['main_end_date']})"
    )
    print(
        f"Euro history: {euro_draw_count:,} draws ({snapshot['euro_start_date']} to {snapshot['euro_end_date']})"
    )
    print(f"Source page: {PUBLIC_STATISTICS_PAGE}")
    print(f"Main five-number combinations: {MAIN_COMBINATIONS:,}")
    print(f"Euro two-number combinations: {EURO_COMBINATIONS:,}")
    print(f"Full 5+2 combinations / jackpot odds: {FULL_COMBINATIONS:,}")
    print(f"Expected main appearances/number: {expected_main:.2f} | σ: {main_sigma:.2f}")
    print(f"Expected Euro appearances/number: {expected_euro:.2f} | σ: {euro_sigma:.2f}")
    print(f"Main sum: expected {MAIN_SUM_MEAN:.2f} | σ {MAIN_SUM_SIGMA:.2f}")
    print(f"Euro sum: expected {EURO_SUM_MEAN:.2f} | σ {EURO_SUM_SIGMA:.2f}")
    print(
        f"Main 3 odd + 2 even lines: {MAIN_ODD_EVEN[3]:,} "
        f"({MAIN_ODD_EVEN[3] / MAIN_COMBINATIONS:.2%}) | 2 odd + 3 even: "
        f"{MAIN_ODD_EVEN[2]:,} ({MAIN_ODD_EVEN[2] / MAIN_COMBINATIONS:.2%})"
    )
    print("\nCommands: analyze, random, best, status, history, drift, backtest, patterns, pairs, draws, import-draws, sync-draws, import-players, players, prizes, ev, export, save, saved, refresh, reseed")


def print_history() -> None:
    snapshot = load_snapshot()
    main_draw_count = int(snapshot["main_draw_count"])
    euro_draw_count = int(snapshot["euro_draw_count"])
    main_stats = frequency_statistics(snapshot["main"], main_draw_count, MAIN_PICK, MAIN_MAX)  # type: ignore[arg-type]
    euro_stats = frequency_statistics(snapshot["euro"], euro_draw_count, EURO_PICK, EURO_MAX)  # type: ignore[arg-type]
    print(f"\nEurojackpot number-frequency and fairness audit\n{'=' * 72}")
    print(f"Source: {snapshot['source_note']} | snapshot updated: {snapshot['updated_at']}")
    print(
        f"Main history: {main_draw_count:,} draws ({snapshot['main_start_date']} to {snapshot['main_end_date']})"
    )
    print(
        f"Euro history: {euro_draw_count:,} draws ({snapshot['euro_start_date']} to {snapshot['euro_end_date']})"
    )
    print("Euro fairness uses only the current 2/12 era; main fairness uses the full 5/50 history.")
    for label, stats in (("Main numbers (1–50)", main_stats), ("Euro numbers (1–12)", euro_stats)):
        counts = stats["counts"]
        expected = float(stats["expected"])
        sigma = float(stats["sigma"])
        z_scores = stats["z"]
        ranked = sorted(range(1, len(counts) + 1), key=lambda number: counts[number - 1], reverse=True)  # type: ignore[index]
        verdict = (
            "A global deviation is flagged at the 1% level; investigate data quality or process."
            if float(stats["p_value"]) < 0.01
            else "No significant global deviation detected under the fair model."
        )
        print(f"\n{label}")
        print(
            f"  expected/number: {expected:.2f} | σ: {sigma:.2f} | "
            f"Pearson χ² (descriptive): {float(stats['pearson_chi_square']):.2f} | "
            f"adjusted χ²: {float(stats['adjusted_chi_square']):.2f} | "
            f"df: {int(stats['degrees_of_freedom'])} | p: {float(stats['p_value']):.4f}"
        )
        print(f"  Verdict: {verdict}")
        print("  Most frequent:")
        for number in ranked[:10]:  # type: ignore[index]
            count = counts[number - 1]  # type: ignore[index]
            print(f"    {number:02}: {count:>4} ({(count - expected) / sigma:+.2f}σ)")
        print("  Least frequent:")
        for number in ranked[-10:][::-1]:  # type: ignore[index]
            count = counts[number - 1]  # type: ignore[index]
            print(f"    {number:02}: {count:>4} ({(count - expected) / sigma:+.2f}σ)")
        print("  All numbers (Bonferroni-adjusted across the family):")
        for number, count, z_score in zip(range(1, len(counts) + 1), counts, z_scores):  # type: ignore[index]
            adjusted_p = bonferroni(normal_two_sided_p(float(z_score)), len(counts))  # type: ignore[arg-type]
            print(
                f"    {number:02} | {int(count):5} | {expected:8.2f} | "
                f"{int(count) - expected:+9.2f} | {float(z_score):+7.2f} | {adjusted_p:.4f}"
            )
    print("\nHistorical fluctuations are descriptive, not predictive.")


def observed_patterns(draws: list[dict[str, object]]) -> dict[str, object]:
    main_odd = [0] * (MAIN_PICK + 1)
    main_low = [0] * (MAIN_PICK + 1)
    main_birthday = [0] * (MAIN_PICK + 1)
    main_adjacency = [0] * MAIN_PICK
    main_sum_counts = [0] * (MAIN_MAX * MAIN_PICK + 1)
    main_range_counts = [0] * MAIN_MAX
    main_sums: list[int] = []
    main_ranges: list[int] = []
    for draw in draws:
        ordered = tuple(sorted(draw["main"]))  # type: ignore[arg-type]
        main_odd[sum(number % 2 for number in ordered)] += 1
        main_low[sum(number <= LOW_MAX for number in ordered)] += 1
        main_birthday[sum(number <= BIRTHDAY_MAX for number in ordered)] += 1
        main_adjacency[adjacent_count(ordered)] += 1
        main_sum = sum(ordered)
        main_range = ordered[-1] - ordered[0]
        main_sums.append(main_sum)
        main_ranges.append(main_range)
        main_sum_counts[main_sum] += 1
        main_range_counts[main_range] += 1
    euro_draws = current_era_draws(draws)
    euro_odd = [0] * (EURO_PICK + 1)
    euro_sum_counts = [0] * (EURO_MAX * EURO_PICK + 1)
    euro_sums: list[int] = []
    for draw in euro_draws:
        ordered = tuple(sorted(draw["euro"]))  # type: ignore[arg-type]
        euro_odd[sum(number % 2 for number in ordered)] += 1
        euro_sum = sum(ordered)
        euro_sums.append(euro_sum)
        euro_sum_counts[euro_sum] += 1
    main_mean = sum(main_sums) / len(main_sums)
    main_sd = math.sqrt(sum((value - main_mean) ** 2 for value in main_sums) / len(main_sums))
    euro_mean = sum(euro_sums) / len(euro_sums)
    euro_sd = math.sqrt(sum((value - euro_mean) ** 2 for value in euro_sums) / len(euro_sums))
    return {
        "main_draws": len(draws),
        "euro_draws": len(euro_draws),
        "main_odd": main_odd,
        "main_low": main_low,
        "main_birthday": main_birthday,
        "main_adjacency": main_adjacency,
        "main_sum_counts": main_sum_counts,
        "main_range_counts": main_range_counts,
        "main_mean": main_mean,
        "main_sd": main_sd,
        "main_median": sorted(main_sums)[len(main_sums) // 2],
        "main_range_mean": sum(main_ranges) / len(main_ranges),
        "euro_odd": euro_odd,
        "euro_sum_counts": euro_sum_counts,
        "euro_mean": euro_mean,
        "euro_sd": euro_sd,
    }


def print_observed_distribution(
    title: str,
    observed: list[int],
    theoretical: list[int],
    draw_count: int,
    total: int,
) -> None:
    expected = [count / total * draw_count for count in theoretical]
    _statistic, degrees_of_freedom, p_value = weighted_chi_square(observed, expected)
    print(f"\n{title}")
    print("  value | observed | expected | difference")
    for value, (observed_count, expected_count) in enumerate(zip(observed, expected)):
        print(f"  {value:5} | {observed_count:8} | {expected_count:8.2f} | {observed_count - expected_count:+8.2f}")
    print(f"  chi-square p = {p_value:.4f} (df={degrees_of_freedom})")


def binned_counts(
    observed_counts: list[int],
    theoretical_counts: list[int],
    draw_count: int,
    total: int,
    start: int,
    width: int = 5,
) -> tuple[list[int], list[float]]:
    observed: list[int] = []
    expected: list[float] = []
    for bin_start in range(start, len(observed_counts), width):
        bin_end = min(bin_start + width, len(observed_counts))
        observed.append(sum(observed_counts[bin_start:bin_end]))
        expected.append(
            sum(theoretical_counts[bin_start:bin_end]) / total * draw_count
        )
    return observed, expected


def print_observed_patterns(draws: list[dict[str, object]]) -> None:
    observed = observed_patterns(draws)
    main_count = int(observed["main_draws"])
    euro_count = int(observed["euro_draws"])
    first_date, last_date = draw_date_range(draws)
    print(f"\nObserved Eurojackpot patterns from imported rows\n{'=' * 72}")
    print(f"Source: {draw_source_summary(draws)}")
    print(f"Main rows: {main_count:,} ({first_date or 'unknown'} to {last_date or 'unknown'})")
    euro_first, euro_last = draw_date_range(current_era_draws(draws))
    print(
        f"Euro rows used: {euro_count:,} ({euro_first or 'unknown'} to {euro_last or 'unknown'}; "
        f"current 2/12 era filter applied where dates exist)"
    )
    print_observed_distribution(
        "Main odd numbers in draw", observed["main_odd"], MAIN_ODD_EVEN, main_count, MAIN_COMBINATIONS  # type: ignore[arg-type]
    )
    print_observed_distribution(
        "Main numbers 1–25 in draw", observed["main_low"], MAIN_LOW_HIGH, main_count, MAIN_COMBINATIONS  # type: ignore[arg-type]
    )
    print_observed_distribution(
        "Main birthday-range numbers 1–31", observed["main_birthday"], MAIN_BIRTHDAY, main_count, MAIN_COMBINATIONS  # type: ignore[arg-type]
    )
    print_observed_distribution(
        "Main adjacent-number pairs", observed["main_adjacency"], MAIN_ADJACENCY, main_count, MAIN_COMBINATIONS  # type: ignore[arg-type]
    )
    if euro_count:
        print_observed_distribution(
            "Euro odd numbers in draw", observed["euro_odd"], EURO_ODD_EVEN, euro_count, EURO_COMBINATIONS  # type: ignore[arg-type]
        )
    print(f"\nMain sum: observed mean {float(observed['main_mean']):.2f} | sd {float(observed['main_sd']):.2f} | "
          f"median {observed['main_median']} | theory mean {MAIN_SUM_MEAN:.2f} | sd {MAIN_SUM_SIGMA:.2f}")
    sum_observed, sum_expected = binned_counts(
        observed["main_sum_counts"], MAIN_SUM_COUNTS, main_count, MAIN_COMBINATIONS, 15  # type: ignore[arg-type]
    )
    _sum_stat, sum_df, sum_p = weighted_chi_square(sum_observed, sum_expected)
    print(f"  binned sum chi-square p = {sum_p:.4f} (df={sum_df})")
    range_observed, range_expected = binned_counts(
        observed["main_range_counts"], MAIN_RANGE, main_count, MAIN_COMBINATIONS, 4  # type: ignore[arg-type]
    )
    _range_stat, range_df, range_p = weighted_chi_square(range_observed, range_expected)
    main_range_mean = sum(index * count for index, count in enumerate(MAIN_RANGE)) / MAIN_COMBINATIONS
    print(f"Main range: observed mean {float(observed['main_range_mean']):.2f} | theory {main_range_mean:.2f}")
    print(f"  binned range chi-square p = {range_p:.4f} (df={range_df})")
    if euro_count:
        print(f"Euro sum: observed mean {float(observed['euro_mean']):.2f} | sd {float(observed['euro_sd']):.2f} "
              f"| theory mean {EURO_SUM_MEAN:.2f} | sd {EURO_SUM_SIGMA:.2f}")
    print("These are descriptive comparisons, not predictions.")


def format_combination(combination: tuple[int, ...]) -> str:
    return ",".join(str(number) for number in combination)


def combination_statistics(
    draws: list[dict[str, object]],
    kind: str,
    size: int,
) -> dict[str, object]:
    if not draws:
        raise ValueError("No draws available for combination statistics.")
    counts: Counter[tuple[int, ...]] = Counter()
    for draw in draws:
        counts.update(combinations(draw[kind], size))  # type: ignore[arg-type]
    draw_count = len(draws)
    number_count = MAIN_MAX if kind == "main" else EURO_MAX
    pick_count = MAIN_PICK if kind == "main" else EURO_PICK
    family = math.comb(number_count, size)
    probability = math.comb(pick_count, size) / family
    expected = draw_count * probability
    standard_deviation = math.sqrt(draw_count * probability * (1 - probability))
    items: list[dict[str, object]] = []
    for combination in combinations(range(1, number_count + 1), size):
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


def print_pair_stats(label: str, statistics: dict[str, object]) -> None:
    print(f"\n{label}\n{'=' * 72}")
    print(
        f"Family size: {statistics['family']:,} | expected per combination: "
        f"{float(statistics['expected']):.2f} | σ: {float(statistics['standard_deviation']):.2f}"
    )
    print("  combination | observed | z-score | exact binomial p | Bonferroni p")
    for item in statistics["top"]:  # type: ignore[union-attr]
        print(
            f"  {format_combination(item['combination']):11} | {item['observed']:8} | "
            f"{item['z_score']:+7.2f} | {item['p_value']:.4f} | {item['adjusted_p']:.4f}"
        )
    print(f"\nLeast frequent {label.lower()}")
    for item in statistics["bottom"]:  # type: ignore[union-attr]
        print(
            f"  {format_combination(item['combination']):11} | {item['observed']:8} | "
            f"{item['z_score']:+7.2f} | {item['p_value']:.4f} | {item['adjusted_p']:.4f}"
        )
    if statistics["never"]:
        print(
            f"\nUnique observed: {statistics['unique']:,} | never observed: {statistics['never']:,} | "
            f"expected never: {float(statistics['expected_never']):.2f}"
        )


def print_pairs() -> None:
    draws = load_draws()
    if not draws:
        raise ValueError("No row-level Eurojackpot draws imported. Run `python3 eurojackpot.py import-draws PATH` first.")
    euro_draws = current_era_draws(draws)
    first_date, last_date = draw_date_range(draws)
    print(f"\nEurojackpot observed pairs/triples\n{'=' * 72}")
    print(f"Source: {draw_source_summary(draws)}")
    print(f"Main rows: {len(draws):,} ({first_date or 'unknown'} to {last_date or 'unknown'})")
    print(f"Euro rows used: {len(euro_draws):,} (current 2/12 era filter)")
    for size, label in ((2, "main pairs"), (3, "main triples")):
        print_pair_stats(label, combination_statistics(draws, "main", size))
    if euro_draws:
        print_pair_stats("Euro pairs", combination_statistics(euro_draws, "euro", 2))
    else:
        print("\nNo imported Euro draws fall in the current 2/12 era; skipping Euro pair analysis.")
    print("\nExact-binomial extremes are exploratory; Bonferroni adjusts across the whole family.")


def print_draws_summary() -> None:
    draws = load_draws()
    if not draws:
        print("No row-level Eurojackpot draws imported.")
        return
    first_date, last_date = draw_date_range(draws)
    print(f"\nImported Eurojackpot draws\n{'=' * 72}")
    print(f"Source: {draw_source_summary(draws)}")
    print(f"Rows: {len(draws):,} ({first_date or 'unknown'} to {last_date or 'unknown'})")
    print(f"Euro rows in the current 2/12 era: {len(current_era_draws(draws)):,}")


def print_patterns() -> None:
    draws = load_draws()
    if draws:
        print_observed_patterns(draws)
    else:
        print("\nNo row-level Eurojackpot draws imported; showing theoretical distributions only.")
        print("Use `python3 eurojackpot.py import-draws PATH` to enable observed patterns.")
    snapshot = load_snapshot()
    main_draw_count = int(snapshot["main_draw_count"])
    euro_draw_count = int(snapshot["euro_draw_count"])
    print(f"\nEurojackpot theoretical pattern distributions\n{'=' * 72}")
    main_sum_min = next(index for index, count in enumerate(MAIN_SUM_COUNTS) if count)
    main_sum_max = max(index for index, count in enumerate(MAIN_SUM_COUNTS) if count)
    main_sum_mode = max(range(len(MAIN_SUM_COUNTS)), key=lambda index: MAIN_SUM_COUNTS[index])
    print(
        f"Main sum: min {main_sum_min} | max {main_sum_max} | mean {MAIN_SUM_MEAN:.2f} | "
        f"σ {MAIN_SUM_SIGMA:.2f} | most common sum {main_sum_mode} "
        f"({MAIN_SUM_COUNTS[main_sum_mode]:,} lines)"
    )
    print("Main odd numbers (count → lines):")
    for odd, count in enumerate(MAIN_ODD_EVEN):
        print(f"  {odd}: {count:>10,} ({count / MAIN_COMBINATIONS:.2%})")
    print("Main low numbers 1–25 (count → lines):")
    for low, count in enumerate(MAIN_LOW_HIGH):
        print(f"  {low}: {count:>10,} ({count / MAIN_COMBINATIONS:.2%})")
    print("Main birthday-range numbers 1–31 (count → lines):")
    for birthday, count in enumerate(MAIN_BIRTHDAY):
        print(f"  {birthday}: {count:>10,} ({count / MAIN_COMBINATIONS:.2%})")
    print("Main adjacent-number pairs (count → lines):")
    for adjacent, count in enumerate(MAIN_ADJACENCY):
        print(f"  {adjacent}: {count:>10,} ({count / MAIN_COMBINATIONS:.2%})")
    main_range_mean = sum(index * count for index, count in enumerate(MAIN_RANGE)) / MAIN_COMBINATIONS
    print(f"Main range: mean {main_range_mean:.2f}")
    print(
        f"Euro sum: min {next(index for index, count in enumerate(EURO_SUM_COUNTS) if count)} | "
        f"max {max(index for index, count in enumerate(EURO_SUM_COUNTS) if count)} | "
        f"mean {EURO_SUM_MEAN:.2f} | σ {EURO_SUM_SIGMA:.2f}"
    )
    print("Euro odd numbers (count → pairs):")
    for odd, count in enumerate(EURO_ODD_EVEN):
        print(f"  {odd}: {count:>3,} ({count / EURO_COMBINATIONS:.2%})")
    reference_main_draws = len(draws) if draws else main_draw_count
    reference_euro_draws = len(current_era_draws(draws)) if draws else euro_draw_count
    print(f"\nMain pair family: {math.comb(MAIN_MAX, 2):,} | expected per pair in the reference history: "
          f"{reference_main_draws * math.comb(MAIN_PICK, 2) / math.comb(MAIN_MAX, 2):.2f}")
    print(f"Main triple family: {math.comb(MAIN_MAX, 3):,} | expected per triple in the reference history: "
          f"{reference_main_draws * math.comb(MAIN_PICK, 3) / math.comb(MAIN_MAX, 3):.2f}")
    print(f"Euro pair family: {EURO_COMBINATIONS:,} | expected per pair in the current Euro era: "
          f"{reference_euro_draws / EURO_COMBINATIONS:.2f}")
    if draws:
        print("\nObserved distributions above use your imported row-level archive; theoretical tables are shown for reference.")
    else:
        print("\nObserved pattern/pair distributions require row-level draw data; this source reports number frequencies only.")


def load_player_tickets() -> list[dict[str, object]]:
    with closing(database_connection()) as connection:
        rows = connection.execute(
            "SELECT main_json, euro_json, source FROM player_ticket ORDER BY id"
        ).fetchall()
    return [
        {
            "main": tuple(json.loads(row["main_json"])),
            "euro": tuple(json.loads(row["euro_json"])),
            "source": str(row["source"]),
        }
        for row in rows
    ]


def import_players(
    path: str | Path, append: bool = False, source: str = "", header_mode: str = "auto"
) -> None:
    records = read_draw_file(path, header_mode=header_mode)
    source_note = source.strip() or str(path)
    with closing(database_connection()) as connection, connection:
        if not append:
            connection.execute("DELETE FROM player_ticket")
        connection.executemany(
            "INSERT INTO player_ticket(main_json, euro_json, source, created_at) VALUES (?, ?, ?, ?)",
            [
                (
                    json.dumps(record["main"]),
                    json.dumps(record["euro"]),
                    source_note,
                    datetime.now(UTC).isoformat(),
                )
                for record in records
            ],
        )
    print(f"Imported {len(records):,} player-selected lines from {source_note}.")


def player_summary(tickets: list[dict[str, object]]) -> dict[str, object]:
    count = len(tickets)
    main_counts = [0] * MAIN_MAX
    euro_counts = [0] * EURO_MAX
    birthday_slots = 0
    combinations_count: Counter[tuple[tuple[int, ...], tuple[int, ...]]] = Counter()
    for ticket in tickets:
        main = tuple(sorted(ticket["main"]))  # type: ignore[arg-type]
        euro = tuple(sorted(ticket["euro"]))  # type: ignore[arg-type]
        combinations_count[(main, euro)] += 1
        for number in main:
            main_counts[number - 1] += 1
            if number <= BIRTHDAY_MAX:
                birthday_slots += 1
        for number in euro:
            euro_counts[number - 1] += 1
    slots = count * MAIN_PICK
    duplicates = [
        {"main": list(combination[0]), "euro": list(combination[1]), "count": occurrences}
        for combination, occurrences in combinations_count.items()
        if occurrences > 1
    ]
    duplicates.sort(key=lambda item: -int(item["count"]))
    top_main = sorted(range(1, MAIN_MAX + 1), key=lambda number: (-main_counts[number - 1], number))[:8]
    top_euro = sorted(range(1, EURO_MAX + 1), key=lambda number: (-euro_counts[number - 1], number))[:5]
    return {
        "tickets": count,
        "slots": slots,
        "birthday_slots": birthday_slots,
        "birthday_share": birthday_slots / slots if slots else 0.0,
        "distinct": len(combinations_count),
        "duplicate_entries": count - len(combinations_count),
        "duplicates": duplicates[:10],
        "top_main": [{"number": n, "count": main_counts[n - 1]} for n in top_main],
        "top_euro": [{"number": n, "count": euro_counts[n - 1]} for n in top_euro],
        "expected_main_per_number": count * MAIN_PICK / MAIN_MAX if count else 0.0,
        "expected_euro_per_number": count * EURO_PICK / EURO_MAX if count else 0.0,
    }


def print_players() -> None:
    tickets = load_player_tickets()
    if not tickets:
        print("\nNo player-selection sample imported. Run `python3 eurojackpot.py import-players PATH` first.")
        print("Without real ticket data, popularity claims remain assumption-based.")
        return
    summary = player_summary(tickets)
    print(f"\nEurojackpot player-selection analysis\n{'=' * 72}")
    print(
        f"Ticket lines: {summary['tickets']:,} | main slots: {summary['slots']:,} | "
        f"expected main selections/number: {summary['expected_main_per_number']:.2f}"
    )
    print(f"Birthday-range slots (1–31): {summary['birthday_slots']:,} ({summary['birthday_share']:.1%})")
    print(
        f"Distinct combinations: {summary['distinct']:,} | duplicate entries beyond first copy: "
        f"{summary['duplicate_entries']:,}"
    )
    print("\nMost selected main numbers in this sample")
    for item in summary["top_main"]:  # type: ignore[union-attr]
        print(f"  {item['number']:2}: {item['count']:,}")
    print("\nMost selected Euro numbers in this sample")
    for item in summary["top_euro"]:  # type: ignore[union-attr]
        print(f"  {item['number']:2}: {item['count']:,}")
    print("\nRepeated combinations")
    if summary["duplicates"]:  # type: ignore[union-attr]
        for item in summary["duplicates"]:  # type: ignore[union-attr]
            print(
                f"  {' '.join(map(str, item['main']))} + {' '.join(map(str, item['euro']))}: "
                f"{item['count']}×"
            )
    else:
        print("  No duplicate combinations in the sample.")
    print("\nThis describes the uploaded sample only and does not change draw probabilities.")


def player_popularity() -> dict[str, object]:
    tickets = load_player_tickets()
    if not tickets:
        raise ValueError("No player-selection data imported. Run `python3 eurojackpot.py import-players PATH` first.")
    main_counts = [0] * MAIN_MAX
    euro_counts = [0] * EURO_MAX
    combinations_count: Counter[tuple[tuple[int, ...], tuple[int, ...]]] = Counter()
    for ticket in tickets:
        main = tuple(sorted(ticket["main"]))  # type: ignore[arg-type]
        euro = tuple(sorted(ticket["euro"]))  # type: ignore[arg-type]
        combinations_count[(main, euro)] += 1
        for number in main:
            main_counts[number - 1] += 1
        for number in euro:
            euro_counts[number - 1] += 1
    main_slots = len(tickets) * MAIN_PICK
    euro_slots = len(tickets) * EURO_PICK
    return {
        "tickets": len(tickets),
        "main_counts": main_counts,
        "euro_counts": euro_counts,
        "main_slots": main_slots,
        "euro_slots": euro_slots,
        "expected_main": main_slots / MAIN_MAX,
        "expected_euro": euro_slots / EURO_MAX,
        "combinations": combinations_count,
    }


def empirical_sharing_score(
    main: tuple[int, ...], euro: tuple[int, ...], popularity: dict[str, object]
) -> tuple[float, dict[str, float]]:
    ordered_main = validate_main(list(main))
    ordered_euro = validate_euro(list(euro))
    expected_main = float(popularity["expected_main"])
    expected_euro = float(popularity["expected_euro"])
    if expected_main <= 0 or expected_euro <= 0:
        raise ValueError("Player-selection sample is empty.")
    main_counts = popularity["main_counts"]
    euro_counts = popularity["euro_counts"]
    main_relative = sum(int(main_counts[number - 1]) for number in ordered_main) / MAIN_PICK / expected_main  # type: ignore[index]
    euro_relative = sum(int(euro_counts[number - 1]) for number in ordered_euro) / EURO_PICK / expected_euro  # type: ignore[index]
    main_factor = max(0.0, min(1.0, (2.0 - main_relative) / 2.0))
    euro_factor = max(0.0, min(1.0, (2.0 - euro_relative) / 2.0))
    duplicate = 0.5 if popularity["combinations"].get((ordered_main, ordered_euro), 0) else 1.0  # type: ignore[union-attr]
    score = 100 * (0.70 * main_factor + 0.30 * euro_factor) * duplicate
    return round(max(0.0, min(100.0, score)), 1), {
        "main_relative_popularity": round(main_relative, 4),
        "euro_relative_popularity": round(euro_relative, 4),
        "duplicate_penalty": duplicate,
    }


def build_results() -> dict[str, object]:
    snapshot = load_snapshot()
    main_stats = frequency_statistics(
        snapshot["main"], int(snapshot["main_draw_count"]), MAIN_PICK, MAIN_MAX  # type: ignore[arg-type]
    )
    euro_stats = frequency_statistics(
        snapshot["euro"], int(snapshot["euro_draw_count"]), EURO_PICK, EURO_MAX  # type: ignore[arg-type]
    )
    draws = load_draws()
    with closing(database_connection()) as connection:
        cross_check = get_metadata(connection, "last_cross_check") or "not recorded"
        cross_check_at = get_metadata(connection, "last_cross_check_at") or ""
    results: dict[str, object] = {
        "game": "Eurojackpot 5/50 + 2/12",
        "source": snapshot["source_note"],
        "snapshot_updated_at": snapshot["updated_at"],
        "cross_check": {"result": cross_check, "at": cross_check_at},
        "main": {
            "draw_count": snapshot["main_draw_count"],
            "start_date": snapshot["main_start_date"],
            "end_date": snapshot["main_end_date"],
            "counts": main_stats["counts"],
            "expected": main_stats["expected"],
            "sigma": main_stats["sigma"],
            "z": main_stats["z"],
            "adjusted_chi_square": main_stats["adjusted_chi_square"],
            "degrees_of_freedom": main_stats["degrees_of_freedom"],
            "p_value": main_stats["p_value"],
        },
        "euro": {
            "draw_count": snapshot["euro_draw_count"],
            "start_date": snapshot["euro_start_date"],
            "end_date": snapshot["euro_end_date"],
            "counts": euro_stats["counts"],
            "expected": euro_stats["expected"],
            "sigma": euro_stats["sigma"],
            "z": euro_stats["z"],
            "adjusted_chi_square": euro_stats["adjusted_chi_square"],
            "degrees_of_freedom": euro_stats["degrees_of_freedom"],
            "p_value": euro_stats["p_value"],
        },
        "theory": {
            "main_sum_mean": MAIN_SUM_MEAN,
            "main_sum_sigma": MAIN_SUM_SIGMA,
            "euro_sum_mean": EURO_SUM_MEAN,
            "euro_sum_sigma": EURO_SUM_SIGMA,
            "main_combinations": MAIN_COMBINATIONS,
            "euro_combinations": EURO_COMBINATIONS,
            "full_combinations": FULL_COMBINATIONS,
        },
    }
    if draws:
        first_date, last_date = draw_date_range(draws)
        results["row_archive"] = {
            "draws": len(draws),
            "from": first_date,
            "to": last_date,
            "sources": draw_source_summary(draws),
            "euro_current_era_draws": len(current_era_draws(draws)),
        }
    return results


def export_csv_rows(results: dict[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    main = results["main"]  # type: ignore[index]
    euro = results["euro"]  # type: ignore[index]
    theory = results["theory"]  # type: ignore[index]
    cross = results["cross_check"]  # type: ignore[index]
    rows.append({
        "section": "metadata", "item": "main_draws", "observed": main["draw_count"],
        "expected": "", "z_score": "", "p_value": "", "adjusted_p": "",
        "notes": f"Main history {main['start_date']} to {main['end_date']}.",
    })
    rows.append({
        "section": "metadata", "item": "euro_draws", "observed": euro["draw_count"],
        "expected": "", "z_score": "", "p_value": "", "adjusted_p": "",
        "notes": f"Euro history {euro['start_date']} to {euro['end_date']}; current 2/12 era.",
    })
    rows.append({
        "section": "metadata", "item": "cross_check", "observed": cross["result"],
        "expected": "", "z_score": "", "p_value": "", "adjusted_p": "",
        "notes": f"Cross-check timestamp {cross['at'] or 'unknown'}.",
    })
    if "row_archive" in results:
        archive = results["row_archive"]  # type: ignore[index]
        rows.append({
            "section": "metadata", "item": "row_archive_draws", "observed": archive["draws"],
            "expected": "", "z_score": "", "p_value": "", "adjusted_p": "",
            "notes": f"{archive['from']} to {archive['to']}; sources: {archive['sources']}.",
        })
    for kind, stats in (("main", main), ("euro", euro)):
        for number, count, z_score in zip(
            range(1, len(stats["counts"]) + 1), stats["counts"], stats["z"]  # type: ignore[index]
        ):
            p_value = normal_two_sided_p(float(z_score))
            rows.append({
                "section": f"{kind}_number_frequency", "item": str(number),
                "observed": count, "expected": round(float(stats["expected"]), 6),
                "z_score": round(float(z_score), 6), "p_value": round(p_value, 8),
                "adjusted_p": round(bonferroni(p_value, len(stats["counts"])), 8),  # type: ignore[index]
                "notes": "Bonferroni-adjusted over the number family.",
            })
        rows.append({
            "section": f"{kind}_fairness", "item": "adjusted_chi_square",
            "observed": round(float(stats["adjusted_chi_square"]), 6),
            "expected": int(stats["degrees_of_freedom"]), "z_score": "",
            "p_value": round(float(stats["p_value"]), 8), "adjusted_p": "",
            "notes": "Adjusted for within-draw dependence.",
        })
    rows.append({
        "section": "theory", "item": "main_sum",
        "observed": round(float(theory["main_sum_mean"]), 6),
        "expected": round(float(theory["main_sum_sigma"]), 6),
        "z_score": "", "p_value": "", "adjusted_p": "",
        "notes": "Main sum mean and standard deviation.",
    })
    rows.append({
        "section": "theory", "item": "euro_sum",
        "observed": round(float(theory["euro_sum_mean"]), 6),
        "expected": round(float(theory["euro_sum_sigma"]), 6),
        "z_score": "", "p_value": "", "adjusted_p": "",
        "notes": "Euro sum mean and standard deviation.",
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


def drift_stats(draws: list[dict[str, object]], kind: str) -> dict[str, object]:
    counts = [0] * (MAIN_MAX if kind == "main" else EURO_MAX)
    for draw in draws:
        for number in draw[kind]:  # type: ignore[union-attr]
            counts[number - 1] += 1
    return frequency_statistics(
        counts,
        len(draws),
        MAIN_PICK if kind == "main" else EURO_PICK,
        MAIN_MAX if kind == "main" else EURO_MAX,
    )


def print_drift(years: int = 10, window_draws: int = 0, window_step: int = 0) -> None:
    if years < 0:
        raise ValueError("--years must be zero or positive.")
    draws = load_draws()
    if not draws:
        raise ValueError("No row-level Eurojackpot draws imported; run `python3 eurojackpot.py import-draws PATH` first.")
    dated = sorted(
        (draw for draw in draws if draw["date"]),
        key=lambda draw: str(draw["date"]),
    )
    if not dated:
        raise ValueError("Drift analysis requires dated draw rows.")
    print(f"\nEurojackpot year-by-year drift\n{'=' * 90}")
    print("Year | main draws | main Q | main p | Euro draws | Euro Q | Euro p | * = Bonferroni-significant")
    available_years = sorted({str(draw["date"])[:4] for draw in dated})
    selected_years = available_years[-years:] if years > 0 else available_years
    year_results = []
    for year in selected_years:
        main_rows = [draw for draw in dated if str(draw["date"]).startswith(year)]
        euro_rows = current_era_draws(main_rows)
        main_stats = drift_stats(main_rows, "main")
        euro_stats = drift_stats(euro_rows, "euro") if euro_rows else None
        year_results.append((year, len(main_rows), main_stats, len(euro_rows), euro_stats))
    year_family = len(year_results)
    for year, main_count, main_stats, euro_count, euro_stats in year_results:
        main_p = float(main_stats["p_value"])
        main_mark = "*" if bonferroni(main_p, year_family) < 0.01 else ""
        if euro_stats is not None:
            euro_p = float(euro_stats["p_value"])
            euro_mark = "*" if bonferroni(euro_p, year_family) < 0.01 else ""
            euro_text = (
                f"{euro_count:10} | {float(euro_stats['adjusted_chi_square']):6.2f} | "
                f"{euro_p:.4f}{euro_mark}"
            )
        else:
            euro_text = "         - |      - |      -"
        print(
            f"{year} | {main_count:10} | {float(main_stats['adjusted_chi_square']):6.2f} | "
            f"{main_p:.4f}{main_mark} | {euro_text}"
        )
    print(f"Bonferroni family size for the yearly scan: {year_family}")
    if window_draws > 0:
        if window_draws < 10:
            raise ValueError("--window-draws must be at least 10.")
        step = window_step if window_step > 0 else window_draws
        if step < 1:
            raise ValueError("--window-step must be positive.")
        print(f"\nWindowed drift (window={window_draws}, step={step})\n{'=' * 90}")
        print("Window | main draws | main Q | main p | Euro draws | Euro Q | Euro p | dates | * = Bonferroni-significant")
        window_results = []
        for index in range(0, len(dated) - window_draws + 1, step):
            block = dated[index:index + window_draws]
            euro_block = current_era_draws(block)
            main_stats = drift_stats(block, "main")
            euro_stats = drift_stats(euro_block, "euro") if euro_block else None
            window_results.append((index // step + 1, block, len(euro_block), main_stats, euro_stats))
        window_family = len(window_results)
        for number, block, euro_count, main_stats, euro_stats in window_results:
            main_p = float(main_stats["p_value"])
            main_mark = "*" if bonferroni(main_p, window_family) < 0.01 else ""
            if euro_stats is not None:
                euro_p = float(euro_stats["p_value"])
                euro_mark = "*" if bonferroni(euro_p, window_family) < 0.01 else ""
                euro_text = (
                    f"{euro_count:10} | {float(euro_stats['adjusted_chi_square']):6.2f} | "
                    f"{euro_p:.4f}{euro_mark}"
                )
            else:
                euro_text = "         - |      - |      -"
            print(
                f"{number:6} | {len(block):10} | "
                f"{float(main_stats['adjusted_chi_square']):6.2f} | {main_p:.4f}{main_mark} | "
                f"{euro_text} | {block[0]['date']} to {block[-1]['date']}"
            )
        print(f"Bonferroni family size for the window scan: {window_family}")
    print("Drift scans are exploratory; many windows increase false-positive risk.")


def print_backtest(samples: int = 5000) -> None:
    draws = load_draws()
    if not draws:
        raise ValueError("No row-level Eurojackpot draws imported; run `python3 eurojackpot.py import-draws PATH` first.")
    if samples <= 0:
        raise ValueError("--samples must be positive.")
    actual = [score_ticket(draw["main"], draw["euro"])[0] for draw in draws]  # type: ignore[arg-type]
    rng = random.Random(20260101)
    random_scores = []
    for _ in range(samples):
        main = tuple(sorted(rng.sample(range(1, MAIN_MAX + 1), MAIN_PICK)))
        euro = tuple(sorted(rng.sample(range(1, EURO_MAX + 1), EURO_PICK)))
        random_scores.append(score_ticket(main, euro)[0])
    actual_mean = sum(actual) / len(actual)
    actual_sd = math.sqrt(sum((value - actual_mean) ** 2 for value in actual) / len(actual))
    random_mean = sum(random_scores) / len(random_scores)
    random_sd = math.sqrt(sum((value - random_mean) ** 2 for value in random_scores) / len(random_scores))
    standard_error = math.sqrt(actual_sd ** 2 / len(actual) + random_sd ** 2 / len(random_scores))
    z_score = (actual_mean - random_mean) / standard_error if standard_error else 0.0
    p_value = normal_two_sided_p(z_score)
    print(f"\nEurojackpot score calibration backtest (anti-sharing profile)\n{'=' * 72}")
    print(f"Historical winning lines: {len(actual):,} | random lines: {len(random_scores):,}")
    print(f"Actual mean score: {actual_mean:.2f} | sd: {actual_sd:.2f} | median: {sorted(actual)[len(actual) // 2]:.1f}")
    print(f"Random mean score: {random_mean:.2f} | sd: {random_sd:.2f} | median: {sorted(random_scores)[len(random_scores) // 2]:.1f}")
    print(f"Difference: {actual_mean - random_mean:+.2f} | z: {z_score:+.2f} | p: {p_value:.4f}")
    print(f"Actual scores ≥75: {sum(value >= 75 for value in actual) / len(actual):.1%} | "
          f"random: {sum(value >= 75 for value in random_scores) / len(random_scores):.1%}")
    print("This tests main-number score calibration against history; it does not create a predictive edge.")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Terminal-only Eurojackpot 5/50 + 2/12 analyzer.")
    parser.add_argument("--offline", action="store_true", help="skip the LOTTO Hessen statistics refresh")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("report", help="show core Eurojackpot probabilities (default)")
    analyze = commands.add_parser("analyze", help="analyze five main plus two Euro numbers")
    analyze.add_argument("numbers", nargs=7, type=int, metavar="N")
    commands.add_parser("random", help="generate and analyze a uniform random Eurojackpot ticket")
    best = commands.add_parser("best", help="generate random tickets and rank them by main-number pattern score")
    best.add_argument("--runs", type=int, default=100, help="number of random tickets (default 100)")
    best.add_argument("--top", type=int, default=0, help="show only the top N tickets (0 = all)")
    best.add_argument("--profile", choices=("typical", "anti-sharing", "empirical"), default="anti-sharing", help="scoring profile (default: anti-sharing)")
    commands.add_parser("status", help="show snapshot, source cross-check, and imported-draw status")
    commands.add_parser("history", help="audit main and Euro number frequencies and fairness")
    drift = commands.add_parser("drift", help="compare fairness across years or rolling windows")
    drift.add_argument("--years", type=int, default=10, help="number of recent years (0 = all)")
    drift.add_argument("--window-draws", type=int, default=0, help="rolling window size")
    drift.add_argument("--window-step", type=int, default=0, help="window step (default: window size)")
    backtest = commands.add_parser("backtest", help="compare score distributions of historical winners and random lines")
    backtest.add_argument("--samples", type=int, default=5000, help="random comparison sample size")
    commands.add_parser("patterns", help="show observed/theoretical Eurojackpot pattern distributions")
    commands.add_parser("pairs", help="analyze imported row-level main pairs/triples and Euro pairs")
    commands.add_parser("draws", help="summarize the imported row-level draw archive")
    import_draws_command = commands.add_parser("import-draws", help="import optional row-level Eurojackpot draws")
    import_draws_command.add_argument("path", help="CSV/JSON with date + five main + two Euro numbers")
    import_draws_command.add_argument("--append", action="store_true", help="append instead of replacing the draw table")
    import_draws_command.add_argument("--source", default="", help="source attribution stored with the rows")
    import_draws_command.add_argument("--header", choices=("auto", "yes", "no"), default="auto", help="CSV header handling")
    import_players_command = commands.add_parser("import-players", help="import a player-selection sample")
    import_players_command.add_argument("path", help="CSV/JSON with five main + two Euro numbers per line")
    import_players_command.add_argument("--append", action="store_true", help="append instead of replacing the sample")
    import_players_command.add_argument("--source", default="", help="source attribution stored with the rows")
    import_players_command.add_argument("--header", choices=("auto", "yes", "no"), default="auto", help="CSV header handling")
    commands.add_parser("players", help="analyze the imported player-selection sample")
    sync = commands.add_parser("sync-draws", help="incrementally import official LOTTO Hessen historic draws")
    sync.add_argument("--years", type=int, default=0, help="number of recent years (0 = all)")
    sync.add_argument("--max-draws", type=int, default=0, help="optional limit for a partial sync")
    commands.add_parser("prizes", help="show official Eurojackpot prize classes and odds")
    ev = commands.add_parser("ev", help="estimate expected value from supplied prize amounts")
    ev.add_argument("--jackpot", type=float, default=0.0, help="class-1 jackpot amount")
    ev.add_argument("--other-prizes", default="", help="comma-separated class 2-12 prize amounts")
    ev.add_argument("--stake", type=float, default=1.0, help="ticket stake/price (default 1.0)")
    export = commands.add_parser("export", help="write a JSON or CSV report")
    export.add_argument("output", help="output file path")
    export.add_argument("--format", choices=("json", "csv"), default="json", help="report format")
    save = commands.add_parser("save", help="save five main plus two Euro numbers locally")
    save.add_argument("numbers", nargs=7, type=int, metavar="N")
    save.add_argument("--label", default="", help="optional local label")
    commands.add_parser("saved", help="list locally saved Eurojackpot tickets")
    commands.add_parser("refresh", help="fetch the LOTTO Hessen statistics immediately")
    commands.add_parser("reseed", help="reload the bundled Eurojackpot statistics")
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    try:
        initialize_database(scrape=not args.offline and args.command not in {"refresh", "reseed"})
        if args.command in (None, "report"):
            print_report()
        elif args.command == "analyze":
            values = args.numbers
            print_ticket_analysis(validate_main(values[:5]), validate_euro(values[5:]))
        elif args.command == "random":
            main, euro = random_ticket()
            print_ticket_analysis(main, euro)
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
        elif args.command == "draws":
            print_draws_summary()
        elif args.command == "import-draws":
            import_draws(args.path, append=args.append, source=args.source, header_mode=args.header)
        elif args.command == "sync-draws":
            sync_draws(args.years, args.max_draws)
        elif args.command == "import-players":
            import_players(args.path, append=args.append, source=args.source, header_mode=args.header)
        elif args.command == "players":
            print_players()
        elif args.command == "prizes":
            print_prizes()
        elif args.command == "ev":
            print_ev(args.jackpot, args.other_prizes, args.stake)
        elif args.command == "export":
            export_results(args.output, args.format)
        elif args.command == "save":
            save_ticket(validate_main(args.numbers[:5]), validate_euro(args.numbers[5:]), args.label)
        elif args.command == "saved":
            list_tickets()
        elif args.command == "refresh":
            if args.offline:
                raise ValueError("The refresh command requires network access; remove --offline.")
            refresh_snapshot()
        elif args.command == "reseed":
            reseed_snapshot()
    except (ValueError, TypeError, OSError, sqlite3.Error) as error:
        raise SystemExit(f"Error: {error}") from error


if __name__ == "__main__":
    main()
