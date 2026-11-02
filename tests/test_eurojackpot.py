import io
import json
import math
import random
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import eurojackpot as ej


class EurojackpotMathTests(unittest.TestCase):
    def test_constants(self) -> None:
        self.assertEqual(ej.MAIN_COMBINATIONS, math.comb(50, 5))
        self.assertEqual(ej.MAIN_COMBINATIONS, 2_118_760)
        self.assertEqual(ej.EURO_COMBINATIONS, math.comb(12, 2))
        self.assertEqual(ej.EURO_COMBINATIONS, 66)
        self.assertEqual(ej.FULL_COMBINATIONS, 139_838_160)

    def test_main_distributions(self) -> None:
        self.assertEqual(sum(ej.MAIN_SUM_COUNTS), ej.MAIN_COMBINATIONS)
        self.assertEqual(sum(ej.MAIN_ODD_EVEN), ej.MAIN_COMBINATIONS)
        self.assertEqual(sum(ej.MAIN_LOW_HIGH), ej.MAIN_COMBINATIONS)
        self.assertEqual(sum(ej.MAIN_BIRTHDAY), ej.MAIN_COMBINATIONS)
        self.assertEqual(sum(ej.MAIN_ADJACENCY), ej.MAIN_COMBINATIONS)
        self.assertEqual(sum(ej.MAIN_RANGE), ej.MAIN_COMBINATIONS)
        self.assertAlmostEqual(ej.MAIN_SUM_MEAN, 127.5, places=6)
        self.assertAlmostEqual(ej.MAIN_SUM_SIGMA, 30.92, places=2)
        adjacency_mean = sum(i * c for i, c in enumerate(ej.MAIN_ADJACENCY)) / ej.MAIN_COMBINATIONS
        self.assertAlmostEqual(adjacency_mean, 0.4, places=6)
        range_mean = sum(i * c for i, c in enumerate(ej.MAIN_RANGE)) / ej.MAIN_COMBINATIONS
        self.assertAlmostEqual(range_mean, 34.0, places=6)
        self.assertEqual(ej.MAIN_RANGE[4], 46)
        self.assertEqual(ej.MAIN_ADJACENCY[4], 46)

    def test_euro_distributions(self) -> None:
        self.assertEqual(sum(ej.EURO_SUM_COUNTS), ej.EURO_COMBINATIONS)
        self.assertEqual(sum(ej.EURO_ODD_EVEN), ej.EURO_COMBINATIONS)
        self.assertAlmostEqual(ej.EURO_SUM_MEAN, 13.0, places=6)
        self.assertEqual(ej.EURO_SUM_COUNTS[3], 1)
        self.assertEqual(ej.EURO_ODD_EVEN[1], 36)

    def test_parse_live_payload(self) -> None:
        euro_counts = [8] * 8 + [9] * 4
        payload = {
            "numbers": [{"number": number, "value": 5} for number in range(1, 51)],
            "additionalNumbers": [
                {"number": number, "value": value} for number, value in enumerate(euro_counts, 1)
            ],
            "drawCount": 50,
            "startDate": 1332457200000,
            "endDate": 1793314800000,
        }
        stats = ej.parse_counts_payload(payload)
        self.assertEqual(stats["main"], [5] * 50)
        self.assertEqual(stats["euro"], euro_counts)
        self.assertEqual(stats["draw_count"], 50)
        self.assertEqual(stats["start_date"], "2012-03-23")
        self.assertEqual(stats["end_date"], "2026-10-30")

    def test_westlotto_parser(self) -> None:
        def barometer(number: int, count: int) -> str:
            return (
                f'<div class="mod mod-barometer"><span class="polygon polygon-fluid barometer">'
                f'<span class="polygon-label">{number}</span></span>'
                f'<div class="mod-barometer__label">{count}</div></div>'
            )

        html = (
            "<p class=\"heading-h5\">5 aus 50</p>"
            + "".join(barometer(number, 100) for number in range(1, 51))
            + "<p class=\"heading-h5\">2 aus 12</p>"
            + "".join(barometer(number, 80) for number in range(1, 13))
            + "Stand: <span>30.10.2026</span>"
        )
        stats = ej.parse_westlotto_html(html)
        self.assertEqual(stats["main"], [100] * 50)
        self.assertEqual(stats["euro"], [80] * 12)
        self.assertEqual(stats["draw_count"], 1000)
        self.assertEqual(stats["start_date"], "2012-03-23")
        self.assertEqual(stats["end_date"], "2026-10-30")
        self.assertIn("WestLotto", str(stats["source_note"]))

    def test_cross_check_mismatch_is_reported(self) -> None:
        main_stats = {
            "main": [100] * 50,
            "euro": [80] * 12,
            "draw_count": 1000,
            "start_date": "2012-03-23",
            "end_date": "2026-10-30",
            "source_note": "fixture",
        }
        euro_stats = {
            "main": [50] * 50,
            "euro": [80] * 12,
            "draw_count": 481,
            "start_date": "2022-03-25",
            "end_date": "2026-10-30",
            "source_note": "fixture",
        }
        west = dict(main_stats)
        west["main"] = [99] * 50

        def fake_lotto(startdate: str | None = None, timeout: float = 8.0) -> dict[str, object]:
            return main_stats if startdate is None else euro_stats

        with mock.patch.object(ej, "fetch_lotto_hessen_stats", side_effect=fake_lotto), mock.patch.object(
            ej, "fetch_westlotto_stats", return_value=west
        ):
            stats = ej.fetch_live_stats()
        self.assertEqual(stats["cross_check"], "mismatch")
        self.assertEqual(stats["main"], main_stats["main"])

    def test_westlotto_is_main_fallback(self) -> None:
        current = ej.parse_counts_payload(json.loads(ej.DEFAULT_CURRENT_COUNTS.read_text(encoding="utf-8")))
        west = {
            "main": [100] * 50,
            "euro": [80] * 12,
            "draw_count": 1000,
            "start_date": "2026-10-30",
            "end_date": "2026-10-30",
            "source_note": "WestLotto Eurojackpot statistics",
        }

        def fake_lotto(startdate: str | None = None, timeout: float = 8.0) -> dict[str, object]:
            if startdate is None:
                raise OSError("primary down")
            return current

        with mock.patch.object(ej, "fetch_lotto_hessen_stats", side_effect=fake_lotto), mock.patch.object(
            ej, "fetch_westlotto_stats", return_value=west
        ):
            stats = ej.fetch_live_stats()
        self.assertEqual(stats["main"], [100] * 50)
        self.assertEqual(stats["euro"], current["euro"])
        self.assertEqual(stats["main_draw_count"], 1000)
        self.assertEqual(stats["euro_draw_count"], current["draw_count"])
        self.assertIn("WestLotto", str(stats["source_note"]))

    def test_parse_rejects_invalid_payloads(self) -> None:
        with self.assertRaises(TypeError):
            ej.parse_counts_payload({"numbers": 1})
        with self.assertRaises(ValueError):
            ej.parse_counts_payload(
                {
                    "numbers": [{"number": number, "value": 5} for number in range(1, 50)],
                    "additionalNumbers": [{"number": number, "value": 2} for number in range(1, 13)],
                    "drawCount": 50,
                }
            )
        with self.assertRaises(ValueError):
            ej.parse_counts_payload(
                {
                    "numbers": [{"number": number, "value": 5} for number in range(1, 51)],
                    "additionalNumbers": [
                        {"number": number, "value": value}
                        for number, value in enumerate([8] * 8 + [9] * 4, 1)
                    ],
                    "drawCount": 50,
                    "startDate": 1.5,
                }
            )
        with self.assertRaises(ValueError):
            ej.parse_counts_payload(
                {
                    "numbers": [{"number": number, "value": 5} for number in range(1, 51)],
                    "additionalNumbers": [{"number": number, "value": 2} for number in range(1, 13)],
                    "drawCount": 0,
                }
            )

    def test_frequency_statistics(self) -> None:
        stats = ej.load_bundled_stats()
        main = ej.frequency_statistics(stats["main"], int(stats["main_draw_count"]), 5, 50)  # type: ignore[arg-type]
        euro = ej.frequency_statistics(stats["euro"], int(stats["euro_draw_count"]), 2, 12)  # type: ignore[arg-type]
        self.assertAlmostEqual(float(main["expected"]), 100.30, places=2)
        self.assertAlmostEqual(float(euro["expected"]), 80.17, places=2)
        self.assertEqual(int(main["degrees_of_freedom"]), 49)
        self.assertEqual(int(euro["degrees_of_freedom"]), 11)
        self.assertAlmostEqual(float(main["p_value"]), 0.8856, places=3)
        self.assertAlmostEqual(float(euro["p_value"]), 0.2059, places=3)

    def test_analyze_ticket(self) -> None:
        analysis = ej.analyze_ticket((1, 2, 3, 4, 5), (1, 2))
        self.assertEqual(analysis["main_sum"], 15)
        self.assertEqual(analysis["main_sum_count"], 1)
        self.assertEqual(analysis["main_odd_count"], 3)
        self.assertEqual(analysis["main_low_count"], 5)
        self.assertEqual(analysis["main_birthday_count"], 5)
        self.assertEqual(analysis["main_adjacent_count"], 4)
        self.assertEqual(analysis["main_longest_run"], 5)
        self.assertEqual(analysis["main_range"], 4)
        self.assertEqual(analysis["main_range_count"], 46)
        self.assertEqual(analysis["euro_sum"], 3)
        self.assertEqual(analysis["euro_sum_count"], 1)
        self.assertEqual(analysis["euro_parity_count"], 36)

    def test_prize_table_and_ev(self) -> None:
        self.assertAlmostEqual(1 / ej.any_prize_probability(), 31.83, places=2)
        jackpot_odds = ej.EURO_PRIZE_CLASSES[0][2]
        result = ej.calculate_ev(jackpot=jackpot_odds, stake=1.0)
        self.assertAlmostEqual(float(result["net"]), 0.0, places=9)
        class_twelve_amount = ej.EURO_PRIZE_FULL_OUTCOMES / ej.EURO_PRIZE_COUNTS[11]
        class_twelve = ej.calculate_ev(0.0, [0.0] * 10 + [class_twelve_amount], 1.0)
        self.assertAlmostEqual(float(class_twelve["expected_value"]), 1.0, places=9)
        self.assertAlmostEqual(float(class_twelve["break_even_jackpot"]), 0.0, places=9)

    def test_parse_prize_amounts(self) -> None:
        self.assertEqual(ej.parse_prize_amounts("", 11), [0.0] * 11)
        self.assertEqual(ej.parse_prize_amounts("1,2", 2), [1.0, 2.0])
        with self.assertRaises(ValueError):
            ej.parse_prize_amounts("1", 2)
        with self.assertRaises(ValueError):
            ej.parse_prize_amounts("-1,2", 2)

    def test_prizes_and_ev_output(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            ej.print_prizes()
            ej.print_ev(10_000_000, ",".join(["1000000"] * 11), 2.0)
        text = output.getvalue()
        self.assertIn("official prize classes", text)
        self.assertIn("Expected return", text)
        self.assertIn("Break-even jackpot", text)

    def test_anti_sharing_profile(self) -> None:
        ticket = (2, 4, 6, 8, 10)
        typical, _ = ej.score_ticket(ticket, (1, 2), "typical")
        anti, components = ej.score_ticket(ticket, (1, 2), "anti-sharing")
        self.assertGreater(anti, typical)
        self.assertEqual(
            set(components),
            {"parity_rarity", "birthday", "adjacency", "range_spread", "decade_spread"},
        )
        with self.assertRaises(ValueError):
            ej.score_ticket(ticket, (1, 2), "unknown")

    def test_score_and_rank(self) -> None:
        low_score, components = ej.score_ticket((1, 2, 3, 4, 5), (1, 2))
        high_score, _ = ej.score_ticket((6, 11, 24, 37, 48), (3, 9))
        self.assertLess(low_score, high_score)
        self.assertGreaterEqual(low_score, 0)
        self.assertLessEqual(high_score, 100)
        self.assertEqual(
            set(components),
            {"parity_rarity", "birthday", "adjacency", "range_spread", "decade_spread"},
        )
        results = ej.generate_ranked_tickets(25, random.Random(7))
        self.assertEqual(len(results), 25)
        scores = [score for score, _main, _euro, _components in results]
        self.assertEqual(scores, sorted(scores, reverse=True))
        for _score, main, euro, _components in results:
            self.assertEqual(len(main), 5)
            self.assertEqual(len(set(main)), 5)
            self.assertEqual(len(euro), 2)
            self.assertEqual(len(set(euro)), 2)
            self.assertTrue(all(1 <= number <= 50 for number in main))
            self.assertTrue(all(1 <= number <= 12 for number in euro))

    def test_score_validation(self) -> None:
        with self.assertRaises(ValueError):
            ej.score_ticket((1, 2, 3, 4), (1, 2))
        with self.assertRaises(ValueError):
            ej.score_ticket((1, 2, 3, 4, 4), (1, 2))
        with self.assertRaises(ValueError):
            ej.score_ticket((1, 2, 3, 4, 5), (1, 1))
        with self.assertRaises(ValueError):
            ej.generate_ranked_tickets(0)


class EurojackpotDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_patch = mock.patch.object(ej, "DATABASE", Path(self.tempdir.name) / "eurojackpot.db")
        self.database_patch.start()
        ej.initialize_database(scrape=False, seed_draws=False)

    def tearDown(self) -> None:
        self.database_patch.stop()
        self.tempdir.cleanup()

    def test_bundled_draws_are_seeded(self) -> None:
        ej.initialize_database(scrape=False)
        draws = ej.load_draws()
        self.assertEqual(len(draws), 1003)
        self.assertEqual(draws[0]["date"], "2012-03-23")
        self.assertEqual(draws[-1]["date"], "2026-10-30")
        self.assertIn("official LOTTO Hessen", str(draws[0]["source"]))
        output = io.StringIO()
        with redirect_stdout(output):
            ej.print_patterns()
            ej.print_pairs()
        text = output.getvalue()
        self.assertIn("Observed Eurojackpot patterns", text)
        self.assertIn("Eurojackpot observed pairs/triples", text)

    def test_snapshot_loads_bundled_data(self) -> None:
        snapshot = ej.load_snapshot()
        self.assertEqual(len(snapshot["main"]), 50)
        self.assertEqual(len(snapshot["euro"]), 12)
        self.assertEqual(snapshot["main_draw_count"], 1003)
        self.assertEqual(snapshot["euro_draw_count"], 481)
        self.assertEqual(sum(snapshot["main"]), 5015)
        self.assertEqual(sum(snapshot["euro"]), 962)
        self.assertEqual(snapshot["main_start_date"], "2012-03-23")
        self.assertEqual(snapshot["main_end_date"], "2026-10-30")
        self.assertEqual(snapshot["euro_start_date"], "2022-03-25")
        self.assertEqual(snapshot["euro_end_date"], "2026-10-30")

    def test_refresh_guard_rejects_smaller_snapshot(self) -> None:
        small = {
            "main": [5] * 50,
            "euro": [2] * 12,
            "main_draw_count": 50,
            "euro_draw_count": 50,
            "main_start_date": "",
            "main_end_date": "",
            "euro_start_date": "",
            "euro_end_date": "",
            "source_note": "small",
        }
        stderr = io.StringIO()
        with mock.patch.object(ej, "fetch_live_stats", return_value=small), redirect_stderr(stderr):
            ej.initialize_database(scrape=True, seed_draws=False)
        self.assertIn("fewer draws than", stderr.getvalue())
        self.assertEqual(ej.load_snapshot()["main_draw_count"], 1003)
        self.assertEqual(ej.load_snapshot()["euro_draw_count"], 481)

    def test_refresh_success_and_reseed(self) -> None:
        stats = ej.load_bundled_stats()
        stats["source_note"] = "test live"
        with mock.patch.object(ej, "fetch_live_stats", return_value=stats):
            output = io.StringIO()
            with redirect_stdout(output):
                ej.refresh_snapshot()
        self.assertIn("refreshed", output.getvalue())
        self.assertEqual(ej.load_snapshot()["source_note"], "test live")
        with closing(ej.database_connection()) as connection, connection:
            connection.execute("DELETE FROM frequency_snapshot WHERE kind = 'euro' AND number = 12")
        with self.assertRaises(ValueError):
            ej.load_snapshot()
        with redirect_stdout(io.StringIO()):
            ej.reseed_snapshot()
        self.assertEqual(ej.load_snapshot()["main_draw_count"], 1003)
        self.assertEqual(ej.load_snapshot()["euro_draw_count"], 481)

    def test_import_draws_and_observed_analysis(self) -> None:
        csv_path = Path(self.tempdir.name) / "eurojackpot.csv"
        csv_path.write_text(
            "date,n1,n2,n3,n4,n5,e1,e2\n"
            "2021-06-01,1,2,3,4,5,1,2\n"
            "2022-03-25,6,12,23,34,45,3,9\n"
            "2022-03-29,7,13,24,35,46,4,10\n",
            encoding="utf-8",
        )
        output = io.StringIO()
        with redirect_stdout(output):
            ej.import_draws(csv_path, source="fixture-source")
        self.assertIn("Imported 3", output.getvalue())
        draws = ej.load_draws()
        self.assertEqual(len(draws), 3)
        self.assertEqual(draws[0]["source"], "fixture-source")
        self.assertEqual(draws[0]["date"], "2021-06-01")
        self.assertEqual(draws[0]["main"], (1, 2, 3, 4, 5))
        self.assertEqual(draws[0]["euro"], (1, 2))
        self.assertEqual(len(ej.current_era_draws(draws)), 2)
        output = io.StringIO()
        with redirect_stdout(output):
            ej.print_draws_summary()
            ej.print_patterns()
            ej.print_pairs()
        text = output.getvalue()
        self.assertIn("Observed Eurojackpot patterns", text)
        self.assertIn("Eurojackpot observed pairs/triples", text)
        self.assertIn("fixture-source", text)

    def test_import_json_and_replace(self) -> None:
        json_path = Path(self.tempdir.name) / "eurojackpot.json"
        json_path.write_text(
            json.dumps({
                "draws": [
                    {"date": "2026-10-27", "main": [1, 2, 3, 4, 5], "euro": [1, 2]},
                    {"date": "2026-10-30", "main": [6, 7, 8, 9, 10], "euro": [3, 4]},
                ]
            }),
            encoding="utf-8",
        )
        with redirect_stdout(io.StringIO()):
            ej.import_draws(json_path, source="json-fixture")
        self.assertEqual(len(ej.load_draws()), 2)
        second_csv = Path(self.tempdir.name) / "one.csv"
        second_csv.write_text("2026-10-30,11,12,13,14,15,5,6\n", encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            ej.import_draws(second_csv)
        self.assertEqual(len(ej.load_draws()), 1)
        with redirect_stdout(io.StringIO()):
            ej.import_draws(second_csv, append=True)
        self.assertEqual(len(ej.load_draws()), 2)

    def test_player_import_and_summary(self) -> None:
        players_csv = Path(self.tempdir.name) / "players.csv"
        players_csv.write_text(
            "n1,n2,n3,n4,n5,e1,e2\n"
            "1,2,3,4,5,1,2\n"
            "1,2,3,4,5,1,2\n"
            "7,8,9,10,11,3,4\n",
            encoding="utf-8",
        )
        with redirect_stdout(io.StringIO()):
            ej.import_players(players_csv, source="player-fixture")
        tickets = ej.load_player_tickets()
        summary = ej.player_summary(tickets)
        self.assertEqual(summary["tickets"], 3)
        self.assertEqual(summary["slots"], 15)
        self.assertEqual(summary["birthday_slots"], 15)
        self.assertEqual(summary["distinct"], 2)
        self.assertEqual(summary["duplicate_entries"], 1)
        self.assertEqual(summary["top_main"][0], {"number": 1, "count": 2})
        output = io.StringIO()
        with redirect_stdout(output):
            ej.print_players()
            ej.print_status()
        text = output.getvalue()
        self.assertIn("Eurojackpot player-selection analysis", text)
        self.assertIn("player-selection lines: 3", text)

    def test_official_sync_draws(self) -> None:
        keys = [
            {"key": "2026-44-5", "eventDate": 1793314800000},
            {"key": "2026-44-2", "eventDate": 1793055600000},
        ]
        records = {
            "2026-44-5": {"date": "2026-10-30", "main": (21, 26, 37, 39, 42), "euro": (5, 7)},
            "2026-44-2": {"date": "2026-10-27", "main": (11, 33, 38, 42, 47), "euro": (11, 12)},
        }
        with mock.patch.object(ej, "fetch_draw_years", return_value=[2026]), mock.patch.object(
            ej, "fetch_draw_keys", return_value=keys
        ), mock.patch.object(ej, "fetch_official_draw", side_effect=lambda key, event_date="", timeout=8.0: records[key]):
            output = io.StringIO()
            with redirect_stdout(output):
                ej.sync_draws(years=1)
            self.assertIn("Imported 2", output.getvalue())
            self.assertEqual(len(ej.load_draws()), 2)
            output = io.StringIO()
            with redirect_stdout(output):
                ej.sync_draws(years=1)
            self.assertIn("Imported 0", output.getvalue())
            self.assertEqual(len(ej.load_draws()), 2)

    def test_draw_parsers(self) -> None:
        records = ej.parse_draw_text("2026-10-30,5,12,23,34,45,3,9\n")
        self.assertEqual(records, [{"date": "2026-10-30", "main": (5, 12, 23, 34, 45), "euro": (3, 9)}])
        records = ej.parse_draw_text(
            '[{"d":"2026-10-30","n":[5,12,23,34,45],"e":[3,9]}]'
        )
        self.assertEqual(records[0]["main"], (5, 12, 23, 34, 45))
        self.assertEqual(records[0]["euro"], (3, 9))
        with self.assertRaises(ValueError):
            ej.parse_draw_text("2026-10-30,1,2,3,4,5,1,1\n")

    def test_status_export_and_saved_tickets(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            ej.print_status()
            ej.save_ticket((5, 12, 23, 34, 45), (3, 9), "fixture")
            ej.save_ticket((5, 12, 23, 34, 45), (3, 9), "duplicate")
            ej.list_tickets()
        text = output.getvalue()
        self.assertIn("Eurojackpot data status", text)
        self.assertIn("Saved locally", text)
        self.assertIn("Already saved", text)
        self.assertIn("5 12 23 34 45 + 3 9", text)
        json_path = Path(self.tempdir.name) / "eurojackpot-export.json"
        csv_path = Path(self.tempdir.name) / "eurojackpot-export.csv"
        with redirect_stdout(io.StringIO()):
            ej.export_results(json_path, "json")
            ej.export_results(csv_path, "csv")
        results = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(results["game"], "Eurojackpot 5/50 + 2/12")
        self.assertIn("main", results)
        self.assertIn("euro", results)
        csv_text = csv_path.read_text(encoding="utf-8-sig")
        self.assertIn("main_number_frequency", csv_text)
        self.assertIn("euro_fairness", csv_text)

    def test_cross_check_status(self) -> None:
        stats = ej.load_bundled_stats()
        stats["cross_check"] = "matched"
        with mock.patch.object(ej, "fetch_live_stats", return_value=stats):
            ej.initialize_database(scrape=True, seed_draws=False)
        output = io.StringIO()
        with redirect_stdout(output):
            ej.print_status()
        self.assertIn("matched", output.getvalue())

    def test_imported_drift_and_backtest(self) -> None:
        csv_path = Path(self.tempdir.name) / "drift.csv"
        csv_path.write_text(
            "date,n1,n2,n3,n4,n5,e1,e2\n"
            "2021-06-01,1,2,3,4,5,1,2\n"
            "2022-03-25,6,12,23,34,45,3,9\n"
            "2022-03-29,7,13,24,35,46,4,10\n",
            encoding="utf-8",
        )
        with redirect_stdout(io.StringIO()):
            ej.import_draws(csv_path, source="drift-fixture")
        output = io.StringIO()
        with redirect_stdout(output):
            ej.print_drift(years=3)
            ej.print_backtest(samples=100)
        text = output.getvalue()
        self.assertIn("year-by-year drift", text)
        self.assertIn("score calibration backtest", text)

    def test_empirical_sharing_score_uses_player_data(self) -> None:
        players_csv = Path(self.tempdir.name) / "empirical_players.csv"
        players_csv.write_text(
            "n1,n2,n3,n4,n5,e1,e2\n"
            "1,2,3,4,5,1,2\n"
            "1,2,3,4,5,1,2\n"
            "7,8,9,10,11,3,4\n",
            encoding="utf-8",
        )
        with redirect_stdout(io.StringIO()):
            ej.import_players(players_csv)
        popularity = ej.player_popularity()
        selected_score, selected_components = ej.empirical_sharing_score((1, 2, 3, 4, 5), (1, 2), popularity)
        unselected_score, unselected_components = ej.empirical_sharing_score((40, 41, 42, 43, 44), (11, 12), popularity)
        self.assertGreater(unselected_score, selected_score)
        self.assertEqual(selected_components["duplicate_penalty"], 0.5)
        self.assertEqual(unselected_components["duplicate_penalty"], 1.0)
        self.assertEqual(
            set(unselected_components),
            {"main_relative_popularity", "euro_relative_popularity", "duplicate_penalty"},
        )
        ranked = ej.generate_ranked_tickets(5, random.Random(1), profile="empirical", popularity=popularity)
        self.assertEqual(len(ranked), 5)
        self.assertEqual([item[0] for item in ranked], sorted([item[0] for item in ranked], reverse=True))

    def test_reports_run(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            ej.print_report()
            ej.print_history()
            ej.print_patterns()
            ej.print_ticket_analysis((5, 12, 23, 34, 45), (3, 9))
            ej.print_ranked_tickets(5, random.Random(2))
        text = output.getvalue()
        self.assertIn("Eurojackpot 5/50 + 2/12", text)
        self.assertIn("Main numbers (1–50)", text)
        self.assertIn("Observed pattern/pair distributions require row-level draw data", text)
        self.assertIn("Full 5+2 jackpot", text)
        self.assertIn("Highest:", text)


if __name__ == "__main__":
    unittest.main()
