import json
import unittest
from pathlib import Path

import app
import eurojackpot as ej

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class FixtureParsingTests(unittest.TestCase):
    def test_lotto_live_statistics_fixture(self) -> None:
        payload = json.loads((FIXTURES / "lotto_live_stats.json").read_text(encoding="utf-8"))
        counts, draw_count, source = app.parse_live_statistics(payload)
        self.assertEqual(counts, [6] * 49)
        self.assertEqual(draw_count, 49)
        self.assertIn("LOTTO.de", source)

    def test_lotto_draw_and_player_fixtures(self) -> None:
        draws = app.parse_data_text((FIXTURES / "lotto_draws.csv").read_text(encoding="utf-8"))
        self.assertEqual(draws, [
            ((1, 2, 3, 4, 5, 6), "2026-10-31"),
            ((1, 2, 3, 4, 5, 7), "2026-11-01"),
        ])
        players = app.parse_data_text((FIXTURES / "lotto_players.csv").read_text(encoding="utf-8"))
        self.assertEqual(len(players), 3)
        self.assertEqual(players[2][0], (7, 8, 9, 10, 11, 32))

    def test_eurojackpot_draw_fixtures(self) -> None:
        csv_records = ej.parse_draw_text((FIXTURES / "eurojackpot_draws.csv").read_text(encoding="utf-8"))
        self.assertEqual(len(csv_records), 3)
        self.assertEqual(csv_records[1]["main"], (6, 12, 23, 34, 45))
        self.assertEqual(csv_records[1]["euro"], (3, 9))
        json_records = ej.parse_draw_text((FIXTURES / "eurojackpot_draws.json").read_text(encoding="utf-8"))
        self.assertEqual(json_records[1]["main"], (6, 7, 8, 9, 10))
        self.assertEqual(json_records[1]["euro"], (3, 4))

    def test_lotto_hessen_api_fixtures(self) -> None:
        full = ej.parse_counts_payload(
            json.loads((FIXTURES / "lotto_hessen_full.json").read_text(encoding="utf-8"))
        )
        current = ej.parse_counts_payload(
            json.loads((FIXTURES / "lotto_hessen_current.json").read_text(encoding="utf-8"))
        )
        self.assertEqual(full["draw_count"], 1003)
        self.assertEqual(current["draw_count"], 481)
        self.assertEqual(sum(full["main"]), 5015)
        self.assertEqual(sum(current["euro"]), 962)

    def test_westlotto_fixture(self) -> None:
        stats = ej.parse_westlotto_html(
            (FIXTURES / "westlotto_eurojackpot.html").read_text(encoding="utf-8")
        )
        self.assertEqual(stats["main"], [100] * 50)
        self.assertEqual(stats["euro"], [80] * 12)
        self.assertEqual(stats["draw_count"], 1000)
        self.assertEqual(stats["start_date"], "2012-03-23")
        self.assertEqual(stats["end_date"], "2026-10-30")


if __name__ == "__main__":
    unittest.main()
