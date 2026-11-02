import io
import json
import math
import random
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import app


class LottoMathTests(unittest.TestCase):
    def test_combination_space_is_official_size(self) -> None:
        self.assertEqual(app.TOTAL_COMBINATIONS, math.comb(49, 6))
        self.assertEqual(app.TOTAL_COMBINATIONS, 13_983_816)

    def test_sum_distribution_totals_and_moments(self) -> None:
        self.assertEqual(sum(app.SUM_COUNTS), app.TOTAL_COMBINATIONS)
        self.assertEqual(app.SUM_MEAN, 150.0)
        self.assertAlmostEqual(app.SUM_SIGMA, 32.79, places=2)

    def test_theoretical_distributions_are_complete(self) -> None:
        for distribution in (
            app.odd_even_distribution(),
            app.low_high_distribution(),
            app.birthday_distribution(),
            app.adjacency_distribution(),
            app.range_distribution(),
        ):
            self.assertEqual(sum(distribution), app.TOTAL_COMBINATIONS)
        self.assertEqual(app.range_distribution()[5], 44)
        self.assertEqual(app.adjacency_distribution()[5], 44)

    def test_adjacency_and_range_means(self) -> None:
        adjacency = app.adjacency_distribution()
        range_counts = app.range_distribution()
        self.assertAlmostEqual(sum(i * c for i, c in enumerate(adjacency)) / app.TOTAL_COMBINATIONS, 30 / 49, places=6)
        self.assertAlmostEqual(sum(i * c for i, c in enumerate(range_counts)) / app.TOTAL_COMBINATIONS, 35.7142857, places=4)

    def test_chi_square_survival_matches_known_values(self) -> None:
        self.assertAlmostEqual(app.chi2_sf(57.5043294420443, 48), 0.16365, places=4)
        self.assertAlmostEqual(app.chi2_sf(51.37655663174541, 48), 0.3429, places=3)

    def test_adjusted_chi_square_uses_fair_model(self) -> None:
        statistic, p_value = app.adjusted_chi_square(list(app.HISTORICAL_FREQUENCIES), app.DRAW_COUNT)
        self.assertAlmostEqual(statistic, 57.5043, places=3)
        self.assertAlmostEqual(p_value, 0.1636, places=3)

    def test_weighted_chi_square_groups_rare_bins(self) -> None:
        observed = [2496, 1987, 529, 44, 4, 0]
        expected = [count / app.TOTAL_COMBINATIONS * app.DRAW_COUNT for count in app.adjacency_distribution()]
        _statistic, degrees_of_freedom, p_value = app.weighted_chi_square(observed, expected)
        self.assertEqual(degrees_of_freedom, 3)
        self.assertAlmostEqual(p_value, 0.2085, places=3)

    def test_exact_binomial_tail_for_triples(self) -> None:
        p_value = app.binomial_two_sided_p(16, app.DRAW_COUNT, app.TRIPLE_PROBABILITY)
        self.assertAlmostEqual(p_value, 0.0003906151, places=9)
        statistics = app.combination_statistics(app.load_default_archive(), 3)
        top = statistics["top"][0]
        self.assertEqual(top["observed"], 16)
        self.assertAlmostEqual(top["p_value"], p_value, places=12)

    def test_extreme_sum_has_one_combination(self) -> None:
        analysis = app.analyze_ticket((1, 2, 3, 4, 5, 6))
        self.assertEqual(analysis["sum"], 21)
        self.assertEqual(analysis["sum_count"], 1)
        self.assertEqual(analysis["longest_run"], 6)
        self.assertEqual(analysis["adjacent_count"], 5)
        self.assertEqual(analysis["birthday_count"], 6)

    def test_central_sum_and_parity_category(self) -> None:
        analysis = app.analyze_ticket((10, 20, 30, 35, 40, 49))
        self.assertEqual(analysis["sum"], 184)
        self.assertEqual(analysis["odd_count"], 2)
        self.assertEqual(analysis["parity_count"], math.comb(25, 2) * math.comb(24, 4))

    def test_ticket_validation(self) -> None:
        self.assertEqual(app.validate_numbers([49, 2, 8, 18, 35, 43]), (2, 8, 18, 35, 43, 49))
        with self.assertRaises(ValueError):
            app.validate_numbers([1, 1, 2, 3, 4, 5])
        with self.assertRaises(ValueError):
            app.validate_numbers([0, 2, 3, 4, 5, 6])

    def test_prize_table_matches_any_prize(self) -> None:
        self.assertAlmostEqual(
            app.table_any_prize_probability(),
            app.any_prize_probability(),
            places=12,
        )
        self.assertAlmostEqual(1 / app.table_any_prize_probability(), 31.3722, places=3)

    def test_prize_ev_break_even(self) -> None:
        jackpot_odds = app.PRIZE_CLASSES[0][2]
        result = app.calculate_ev(jackpot=jackpot_odds, stake=1.0)
        self.assertAlmostEqual(float(result["net"]), 0.0, places=9)
        class_nine_amount = app.PRIZE_FULL_OUTCOMES / app.PRIZE_COUNTS[8]
        class_nine = app.calculate_ev(0.0, [0.0] * 7 + [class_nine_amount], 1.0)
        self.assertAlmostEqual(float(class_nine["expected_value"]), 1.0, places=9)
        self.assertAlmostEqual(float(class_nine["break_even_jackpot"]), 0.0, places=9)

    def test_parse_prize_amounts(self) -> None:
        self.assertEqual(app.parse_prize_amounts("", 8), [0.0] * 8)
        self.assertEqual(app.parse_prize_amounts("1,2,3", 3), [1.0, 2.0, 3.0])
        with self.assertRaises(ValueError):
            app.parse_prize_amounts("1,2", 3)
        with self.assertRaises(ValueError):
            app.parse_prize_amounts("-1,2,3", 3)
        with self.assertRaises(ValueError):
            app.parse_prize_amounts("x,2,3", 3)

    def test_prizes_and_ev_output(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            app.print_prizes()
            app.print_ev(10_000_000, "1000000,50000,10000,5000,1000,100,10,5", 1.2)
        text = output.getvalue()
        self.assertIn("official prize classes", text)
        self.assertIn("Expected return", text)
        self.assertIn("Break-even jackpot", text)

    def test_report_categories_are_derived_consistently(self) -> None:
        self.assertEqual(app.CONSECUTIVE_SIX, 44)
        self.assertEqual(app.one_in(app.CONSECUTIVE_SIX), "1 in 317,814")
        self.assertAlmostEqual(1 / app.any_prize_probability(), 31.37, places=2)

    def test_assessment_flags_popular_patterns(self) -> None:
        flags = app.assess_ticket((1, 2, 3, 4, 5, 6))
        messages = " ".join(message for _level, message in flags).lower()
        self.assertIn("birthday", messages)
        self.assertIn("consecutive", messages)
        self.assertIn("adjacent", messages)
        neutral = app.assess_ticket((10, 17, 24, 31, 42, 49))
        self.assertEqual(neutral[0][0], "good")


class ParsingTests(unittest.TestCase):
    def test_csv_with_header(self) -> None:
        text = "date,n1,n2,n3,n4,n5,n6,superzahl\n2026-10-31,1,2,3,4,5,6,7\n"
        self.assertEqual(app.parse_data_text(text), [((1, 2, 3, 4, 5, 6), "2026-10-31")])

    def test_csv_without_header(self) -> None:
        text = "2026-10-31,6,5,4,3,2,1,7\n"
        self.assertEqual(app.parse_data_text(text), [((1, 2, 3, 4, 5, 6), "2026-10-31")])

    def test_json_draws_shape(self) -> None:
        text = '{"draws": [{"d": "2026-10-31", "n": [3, 1, 2, 6, 5, 4]}]}'
        self.assertEqual(app.parse_data_text(text), [((1, 2, 3, 4, 5, 6), "2026-10-31")])

    def test_json_array_shape(self) -> None:
        text = '[[7, 8, 9, 10, 11, 12]]'
        self.assertEqual(app.parse_data_text(text), [((7, 8, 9, 10, 11, 12), "")])

    def test_duplicate_numbers_are_rejected(self) -> None:
        self.assertIsNone(app.pick_six([1, 1, 2, 3, 4, 5, 6]))
        self.assertIsNone(app.pick_six([1, 2, 3, 4, 5, 5]))

    def test_headerless_index_column(self) -> None:
        text = "1,5,12,23,34,45,49\n2,6,13,24,35,46,48\n"
        records = app.parse_data_text(text)
        self.assertEqual(records[0][0], (5, 12, 23, 34, 45, 49))
        self.assertEqual(records[1][0], (6, 13, 24, 35, 46, 48))

    def test_headerless_index_column_with_superzahl(self) -> None:
        text = "1,5,12,23,34,45,49,7\n2,6,13,24,35,46,48,8\n"
        records = app.parse_data_text(text)
        self.assertEqual(records[0][0], (5, 12, 23, 34, 45, 49))
        self.assertEqual(records[1][0], (6, 13, 24, 35, 46, 48))

    def test_headerless_index_column_with_date(self) -> None:
        text = "1,2026-10-31,5,12,23,34,45,49\n2,2026-11-01,6,13,24,35,46,48\n"
        records = app.parse_data_text(text)
        self.assertEqual(records[0], ((5, 12, 23, 34, 45, 49), "2026-10-31"))
        self.assertEqual(records[1], ((6, 13, 24, 35, 46, 48), "2026-11-01"))

    def test_single_draw_json_shapes(self) -> None:
        self.assertEqual(app.parse_data_text("[1,2,3,4,5,6]"), [((1, 2, 3, 4, 5, 6), "")])
        self.assertEqual(app.parse_data_text('{"n":[1,2,3,4,5,6]}'), [((1, 2, 3, 4, 5, 6), "")])
        self.assertEqual(
            app.parse_data_text('{"numbers":[1,2,3,4,5,6],"date":"2026-10-31"}'),
            [((1, 2, 3, 4, 5, 6), "2026-10-31")],
        )

    def test_numeric_only_header_is_skipped(self) -> None:
        self.assertEqual(
            app.parse_data_text("1,2,3,4,5,6\n2026-10-31,5,12,23,34,45,49\n"),
            [((5, 12, 23, 34, 45, 49), "2026-10-31")],
        )
        self.assertEqual(
            app.parse_data_text("1,2,3,4,5,6\n5,12,23,34,45,49\n"),
            [((5, 12, 23, 34, 45, 49), "")],
        )
        self.assertEqual(
            app.parse_data_text("1,2,3,4,5,6,7\n5,12,23,34,45,49,7\n"),
            [((5, 12, 23, 34, 45, 49), "")],
        )
        self.assertEqual(
            app.parse_data_text("10,20,30,40,50,60\n2026-10-31,5,12,23,34,45,49\n"),
            [((5, 12, 23, 34, 45, 49), "2026-10-31")],
        )
        # A single valid row is still treated as a draw, not a header.
        self.assertEqual(app.parse_data_text("1,2,3,4,5,6"), [((1, 2, 3, 4, 5, 6), "")])

    def test_invalid_files_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            app.parse_data_text("")
        with self.assertRaises(ValueError):
            app.parse_data_text("date,n1,n2,n3,n4,n5,n6\n")
        with self.assertRaises(TypeError):
            app.parse_data_text('{"draws": 1}')

    def test_header_mode_override(self) -> None:
        text = "1,2,3,4,5,6\n5,12,23,34,45,49\n"
        self.assertEqual(
            app.parse_data_text(text, header_mode="yes"),
            [((5, 12, 23, 34, 45, 49), "")],
        )
        self.assertEqual(len(app.parse_data_text(text, header_mode="no")), 2)

    def test_date_formats_are_normalized(self) -> None:
        self.assertEqual(
            app.parse_data_text("31.10.2026,1,2,3,4,5,6\n"),
            [((1, 2, 3, 4, 5, 6), "2026-10-31")],
        )
        self.assertEqual(
            app.parse_data_text('{"draws":[{"d":"31.10.2026","n":[1,2,3,4,5,6]}]}'),
            [((1, 2, 3, 4, 5, 6), "2026-10-31")],
        )

    def test_legacy_wednesday_filter(self) -> None:
        records = [
            ((1, 2, 3, 4, 5, 6), "2000-11-29"),
            ((1, 2, 3, 4, 5, 7), "2000-12-06"),
            ((1, 2, 3, 4, 5, 8), "1955-10-09"),
            ((1, 2, 3, 4, 5, 9), ""),
        ]
        filtered = app.comparable_draws(records)
        self.assertEqual([numbers for numbers, _ in filtered], [
            (1, 2, 3, 4, 5, 7),
            (1, 2, 3, 4, 5, 8),
            (1, 2, 3, 4, 5, 9),
        ])


class SnapshotDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_patch = mock.patch.object(app, "DATABASE", Path(self.tempdir.name) / "test.db")
        self.database_patch.start()
        app.initialize_database(seed_draws=False, scrape=False)

    def tearDown(self) -> None:
        self.database_patch.stop()
        self.tempdir.cleanup()

    def test_snapshot_loads_supplied_draw_count(self) -> None:
        draw_count, rows = app.load_snapshot()
        self.assertEqual(draw_count, app.DRAW_COUNT)
        self.assertEqual([row["number"] for row in rows], list(range(1, 50)))

    def test_load_snapshot_uses_stored_draw_count(self) -> None:
        with closing(app.database_connection()) as connection, connection:
            connection.execute("UPDATE frequency_snapshot SET draw_count = 5060")
        draw_count, _ = app.load_snapshot()
        self.assertEqual(draw_count, 5060)

    def test_save_and_list_ticket(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            app.save_ticket((1, 2, 3, 4, 5, 6), "test")
            app.save_ticket((1, 2, 3, 4, 5, 6), "duplicate")
            app.list_tickets()
        printed = output.getvalue()
        self.assertIn("Saved locally", printed)
        self.assertIn("Already saved", printed)
        self.assertIn("1 2 3 4 5 6 — test", printed)

    def test_incomplete_snapshot_is_rejected_and_can_be_reseeded(self) -> None:
        with closing(app.database_connection()) as connection, connection:
            connection.execute("DELETE FROM frequency_snapshot WHERE number = 49")
        with self.assertRaises(ValueError):
            app.load_snapshot()
        with redirect_stdout(io.StringIO()):
            app.reseed_snapshot()
        draw_count, rows = app.load_snapshot()
        self.assertEqual(draw_count, app.DRAW_COUNT)
        self.assertEqual(len(rows), 49)


class ImportAndAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.directory = Path(self.tempdir.name)
        self.database_patch = mock.patch.object(app, "DATABASE", self.directory / "test.db")
        self.database_patch.start()
        app.initialize_database(seed_draws=False, scrape=False)
        self.draws_csv = self.directory / "draws.csv"
        self.draws_csv.write_text(
            "date,n1,n2,n3,n4,n5,n6\n"
            "2026-10-31,1,2,3,4,5,6\n"
            "2026-11-01,1,2,3,4,5,7\n"
            "2026-11-02,1,2,3,4,5,8\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.database_patch.stop()
        self.tempdir.cleanup()

    def test_import_draws_and_frequency_counts(self) -> None:
        with redirect_stdout(io.StringIO()):
            app.import_draws(self.draws_csv)
        draws = app.load_draws()
        self.assertEqual(len(draws), 3)
        counts, draw_count, source = app.frequency_counts()
        self.assertEqual(draw_count, 3)
        self.assertEqual(source, "imported draw archive")
        self.assertEqual(counts[0], 3)
        self.assertEqual(counts[7], 1)

    def test_pair_and_triple_statistics(self) -> None:
        with redirect_stdout(io.StringIO()):
            app.import_draws(self.draws_csv)
        draws = app.load_draws()
        pairs = app.combination_statistics(draws, 2)
        triples = app.combination_statistics(draws, 3)
        self.assertEqual(pairs["family"], 1176)
        self.assertEqual(pairs["top"][0]["combination"], (1, 2))
        self.assertEqual(pairs["top"][0]["observed"], 3)
        self.assertEqual(triples["family"], 18_424)
        self.assertEqual(triples["top"][0]["observed"], 3)
        self.assertGreater(triples["never"], 0)

    def test_observed_patterns(self) -> None:
        with redirect_stdout(io.StringIO()):
            app.import_draws(self.draws_csv)
        patterns = app.observed_patterns(app.load_draws())
        self.assertEqual(patterns["odd"][3], 2)
        self.assertEqual(patterns["odd"][4], 1)
        self.assertEqual(patterns["low"][6], 3)
        self.assertEqual(patterns["birthday"][6], 3)
        self.assertEqual(patterns["adjacency"][5], 1)
        self.assertEqual(patterns["adjacency"][4], 2)
        self.assertAlmostEqual(patterns["sum_mean"], 22.0)
        self.assertAlmostEqual(patterns["range_mean"], 6.0)

    def test_reports_run_without_crashing(self) -> None:
        with redirect_stdout(io.StringIO()):
            app.import_draws(self.draws_csv)
        output = io.StringIO()
        with redirect_stdout(output):
            app.print_history()
            app.print_patterns()
            app.print_pairs()
            app.print_players()
        printed = output.getvalue()
        self.assertIn("adjusted χ²", printed)
        self.assertIn("Most frequent pairs", printed)
        self.assertIn("No player-selection data", printed)

    def test_player_import_and_summary(self) -> None:
        players_csv = self.directory / "players.csv"
        players_csv.write_text(
            "n1,n2,n3,n4,n5,n6\n"
            "1,2,3,4,5,6\n"
            "1,2,3,4,5,6\n"
            "7,8,9,10,11,32\n",
            encoding="utf-8",
        )
        with redirect_stdout(io.StringIO()):
            app.import_players(players_csv)
        tickets = app.load_player_tickets()
        summary = app.player_summary(tickets)
        self.assertEqual(summary["tickets"], 3)
        self.assertEqual(summary["slots"], 18)
        self.assertEqual(summary["birthday_slots"], 17)
        self.assertEqual(summary["distinct"], 2)
        self.assertEqual(summary["duplicate_entries"], 1)
        self.assertEqual(summary["top"][0], {"number": 1, "count": 2})
        output = io.StringIO()
        with redirect_stdout(output):
            app.print_players()
        self.assertIn("Player-selection analysis", output.getvalue())

    def test_empirical_sharing_score_uses_player_data(self) -> None:
        players_csv = self.directory / "empirical_players.csv"
        players_csv.write_text(
            "n1,n2,n3,n4,n5,n6\n"
            "1,2,3,4,5,6\n"
            "1,2,3,4,5,6\n"
            "7,8,9,10,11,12\n",
            encoding="utf-8",
        )
        with redirect_stdout(io.StringIO()):
            app.import_players(players_csv)
        popularity = app.player_popularity()
        selected_score, selected_components = app.empirical_sharing_score((1, 2, 3, 4, 5, 6), popularity)
        unselected_score, unselected_components = app.empirical_sharing_score((40, 41, 42, 43, 44, 45), popularity)
        self.assertGreater(unselected_score, selected_score)
        self.assertEqual(selected_components["duplicate_penalty"], 0.5)
        self.assertEqual(unselected_components["duplicate_penalty"], 1.0)
        self.assertEqual(set(unselected_components), {"relative_popularity", "duplicate_penalty"})
        ranked = app.generate_ranked_tickets(5, random.Random(1), profile="empirical", popularity=popularity)
        self.assertEqual(len(ranked), 5)
        self.assertEqual([item[0] for item in ranked], sorted([item[0] for item in ranked], reverse=True))

    def test_export_json_and_csv(self) -> None:
        with redirect_stdout(io.StringIO()):
            app.import_draws(self.draws_csv)
        json_path = self.directory / "results.json"
        csv_path = self.directory / "results.csv"
        with redirect_stdout(io.StringIO()):
            app.export_results(json_path, "json")
            app.export_results(csv_path, "csv")
        results = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(results["draws"], 3)
        self.assertIn("fairness", results)
        self.assertIn("patterns", results)
        self.assertIn("pairs", results)
        self.assertIn("triples", results)
        csv_text = csv_path.read_text(encoding="utf-8-sig")
        self.assertIn("section,item,observed", csv_text)
        self.assertIn("number_frequency", csv_text)
        with self.assertRaises(ValueError):
            app.export_results(self.directory / "bad.txt", "xml")


class BundledArchiveTests(unittest.TestCase):
    def test_archive_reconciles_snapshot(self) -> None:
        archive = app.load_default_archive()
        self.assertEqual(len(archive), app.DRAW_COUNT)
        counts = [0] * 49
        for numbers, _ in archive:
            for number in numbers:
                counts[number - 1] += 1
        self.assertEqual(tuple(counts), app.HISTORICAL_FREQUENCIES)
        self.assertEqual(archive[0][1], "1955-10-09")
        self.assertEqual(archive[-1][1], "2026-10-31")

    def test_default_initialization_seeds_draws(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            app, "DATABASE", Path(directory) / "test.db"
        ):
            app.initialize_database(scrape=False)
            self.assertEqual(len(app.load_draws()), app.DRAW_COUNT)
            self.assertEqual(app.frequency_counts()[1], app.DRAW_COUNT)
            self.assertEqual(app.frequency_counts()[2], "Supplied LOTTO.de frequency snapshot")

    def test_status_drift_and_backtest(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            app, "DATABASE", Path(directory) / "test.db"
        ):
            app.initialize_database(scrape=False)
            output = io.StringIO()
            with redirect_stdout(output):
                app.print_status()
                app.print_drift(years=3)
                app.print_backtest(samples=200)
            text = output.getvalue()
            self.assertIn("LOTTO 6aus49 data status", text)
            self.assertIn("year-by-year drift", text)
            self.assertIn("score calibration backtest", text)

    def test_bundled_archive_refreshes_when_fingerprint_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            app, "DATABASE", Path(directory) / "test.db"
        ):
            app.initialize_database(scrape=False)
            with closing(app.database_connection()) as connection:
                first_fingerprint = app.get_metadata(connection, "bundled_archive_sha256")
            self.assertIsNotNone(first_fingerprint)
            with mock.patch.object(app, "archive_fingerprint", return_value="different"):
                app.initialize_database(scrape=False)
            with closing(app.database_connection()) as connection:
                second_fingerprint = app.get_metadata(connection, "bundled_archive_sha256")
            self.assertEqual(second_fingerprint, "different")
            self.assertEqual(len(app.load_draws()), app.DRAW_COUNT)

    def test_bundled_refresh_does_not_overwrite_user_imports(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            app, "DATABASE", Path(directory) / "test.db"
        ):
            app.initialize_database(scrape=False)
            csv_path = Path(directory) / "draws.csv"
            csv_path.write_text(
                "date,n1,n2,n3,n4,n5,n6\n"
                "2026-10-31,1,2,3,4,5,6\n"
                "2026-11-01,1,2,3,4,5,7\n",
                encoding="utf-8",
            )
            with redirect_stdout(io.StringIO()):
                app.import_draws(csv_path)
            with mock.patch.object(app, "archive_fingerprint", return_value="different"):
                app.initialize_database(scrape=False)
            self.assertEqual(len(app.load_draws()), 2)

    def test_corrupt_archive_warns_and_keeps_snapshot(self) -> None:
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            app, "DATABASE", Path(directory) / "test.db"
        ), mock.patch.object(app, "load_default_archive", side_effect=ValueError("bad archive")), redirect_stderr(stderr):
            app.initialize_database(scrape=False)
            self.assertIn("bundled draw archive unavailable", stderr.getvalue())
            self.assertEqual(app.load_snapshot()[0], app.DRAW_COUNT)
            self.assertEqual(app.load_draws(), [])

    def test_export_uses_row_archive_sample_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            app, "DATABASE", Path(directory) / "test.db"
        ):
            app.initialize_database(scrape=False)
            app.store_snapshot([6] * 49, 49, "synthetic snapshot")
            results = app.build_results()
            self.assertEqual(results["fairness_draws"], 49)
            self.assertEqual(results["row_archive_draws"], app.DRAW_COUNT)
            rows = app.export_csv_rows(results)
            odd_six = next(
                row for row in rows if row["section"] == "odd_count" and row["item"] == "6"
            )
            expected = app.DRAW_COUNT * app.ODD_EVEN_COUNTS[6] / app.TOTAL_COMBINATIONS
            self.assertAlmostEqual(odd_six["expected"], expected, places=6)
            metadata = {
                row["item"]: row["observed"]
                for row in rows
                if row["section"] == "metadata"
            }
            self.assertEqual(metadata["fairness_draws"], 49)
            self.assertEqual(metadata["row_archive_draws"], app.DRAW_COUNT)
            self.assertTrue(metadata["snapshot_updated_at"])

    def test_report_includes_snapshot_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            app, "DATABASE", Path(directory) / "test.db"
        ):
            app.initialize_database(scrape=False)
            self.assertTrue(app.snapshot_updated_at())
            output = io.StringIO()
            with redirect_stdout(output):
                app.print_report()
            self.assertIn("snapshot updated:", output.getvalue())
            results = app.build_results()
            self.assertTrue(results["snapshot_updated_at"])


class LiveSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_patch = mock.patch.object(app, "DATABASE", Path(self.tempdir.name) / "test.db")
        self.database_patch.start()

    def tearDown(self) -> None:
        self.database_patch.stop()
        self.tempdir.cleanup()

    def test_parse_live_payload(self) -> None:
        payload = {"counts": [{"drawNumber": number, "count": 6} for number in range(1, 50)]}
        counts, draw_count, source = app.parse_live_statistics(payload)
        self.assertEqual(counts, [6] * 49)
        self.assertEqual(draw_count, 49)
        self.assertIn("LOTTO.de", source)

    def test_parse_rejects_invalid_payloads(self) -> None:
        with self.assertRaises(ValueError):
            app.parse_live_statistics({"counts": [{"drawNumber": 1, "count": 6}]})
        with self.assertRaises(TypeError):
            app.parse_live_statistics({"counts": 1})
        with self.assertRaises(ValueError):
            app.parse_live_statistics({"counts": [{"drawNumber": number, "count": 5} for number in range(1, 50)]})

    def test_parse_rejects_floats_and_zero_counts(self) -> None:
        with self.assertRaises(ValueError):
            app.parse_live_statistics({"counts": [{"drawNumber": 1.5, "count": 6} for _ in range(49)]})
        with self.assertRaises(ValueError):
            app.parse_live_statistics({"counts": [{"drawNumber": number, "count": 6.9} for number in range(1, 50)]})
        with self.assertRaises(ValueError):
            app.parse_live_statistics({"counts": [{"drawNumber": number, "count": 0} for number in range(1, 50)]})

    def test_store_snapshot_validates(self) -> None:
        with self.assertRaises(ValueError):
            app.store_snapshot([0] * 49, 1, "bad")

    def test_initialize_rejects_smaller_live_snapshot(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(
            app, "fetch_live_snapshot", return_value=([6] * 49, 49, "stale live")
        ), redirect_stderr(stderr):
            app.initialize_database(seed_draws=False, scrape=True)
        self.assertIn("fewer than", stderr.getvalue())
        draw_count, rows = app.load_snapshot()
        self.assertEqual(draw_count, app.DRAW_COUNT)
        self.assertEqual(rows[0]["source_note"], "Supplied LOTTO.de frequency snapshot")

    def test_initialize_survives_store_failure(self) -> None:
        counts = list(app.HISTORICAL_FREQUENCIES)
        stderr = io.StringIO()
        with mock.patch.object(
            app, "fetch_live_snapshot", return_value=(counts, app.DRAW_COUNT, "LOTTO.de live statistics")
        ), mock.patch.object(app, "store_snapshot", side_effect=ValueError("store failed")), redirect_stderr(stderr):
            app.initialize_database(seed_draws=False, scrape=True)
        self.assertIn("could not store", stderr.getvalue())
        self.assertEqual(app.load_snapshot()[0], app.DRAW_COUNT)

    def test_initialize_refreshes_snapshot(self) -> None:
        counts = list(app.HISTORICAL_FREQUENCIES)
        with mock.patch.object(
            app, "fetch_live_snapshot", return_value=(counts, app.DRAW_COUNT, "LOTTO.de live statistics")
        ):
            app.initialize_database(seed_draws=False, scrape=True)
        draw_count, rows = app.load_snapshot()
        self.assertEqual(draw_count, app.DRAW_COUNT)
        self.assertEqual(rows[0]["source_note"], "LOTTO.de live statistics")
        snapshot_counts, snapshot_draws, source = app.frequency_counts()
        self.assertEqual(snapshot_counts, counts)
        self.assertEqual(snapshot_draws, app.DRAW_COUNT)
        self.assertEqual(source, "LOTTO.de live statistics")

    def test_scrape_failure_keeps_bundled_snapshot(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(app, "fetch_live_snapshot", side_effect=OSError("offline")), redirect_stderr(stderr):
            app.initialize_database(seed_draws=False, scrape=True)
        draw_count, rows = app.load_snapshot()
        self.assertEqual(draw_count, app.DRAW_COUNT)
        self.assertEqual(rows[0]["source_note"], "Supplied LOTTO.de frequency snapshot")
        self.assertIn("refresh failed", stderr.getvalue())

    def test_refresh_snapshot_updates_database(self) -> None:
        counts = list(app.HISTORICAL_FREQUENCIES)
        with mock.patch.object(
            app, "fetch_live_snapshot", return_value=(counts, app.DRAW_COUNT, "LOTTO.de live statistics")
        ):
            app.initialize_database(seed_draws=False, scrape=False)
            output = io.StringIO()
            with redirect_stdout(output):
                app.refresh_snapshot()
        self.assertIn("refreshed", output.getvalue())
        self.assertEqual(app.load_snapshot()[0], app.DRAW_COUNT)

    def test_refresh_rejects_smaller_live_snapshot(self) -> None:
        with mock.patch.object(
            app, "fetch_live_snapshot", return_value=([6] * 49, 49, "stale live")
        ):
            app.initialize_database(seed_draws=False, scrape=False)
            with self.assertRaises(ValueError):
                app.refresh_snapshot()
        self.assertEqual(app.load_snapshot()[0], app.DRAW_COUNT)


class RankingTests(unittest.TestCase):
    def test_score_rewards_balanced_ticket(self) -> None:
        low_score, components = app.score_ticket((1, 2, 3, 4, 5, 6), "typical")
        high_score, _high_components = app.score_ticket((4, 11, 19, 32, 38, 46), "typical")
        self.assertLess(low_score, high_score)
        self.assertGreaterEqual(low_score, 0)
        self.assertLessEqual(high_score, 100)
        self.assertEqual(
            set(components),
            {"sum", "parity", "low_high", "range", "adjacency", "birthday"},
        )

    def test_anti_sharing_profile(self) -> None:
        ticket = (2, 4, 6, 8, 10, 12)
        typical, _ = app.score_ticket(ticket, "typical")
        anti, components = app.score_ticket(ticket, "anti-sharing")
        self.assertGreater(anti, typical)
        self.assertEqual(
            set(components),
            {"parity_rarity", "birthday", "adjacency", "range_spread", "decade_spread"},
        )
        with self.assertRaises(ValueError):
            app.score_ticket(ticket, "unknown")

    def test_generate_ranked_tickets_sorted(self) -> None:
        results = app.generate_ranked_tickets(25, random.Random(7))
        self.assertEqual(len(results), 25)
        scores = [score for score, _numbers, _components in results]
        self.assertEqual(scores, sorted(scores, reverse=True))
        for score, numbers, components in results:
            self.assertEqual(len(numbers), 6)
            self.assertEqual(len(set(numbers)), 6)
            self.assertTrue(all(1 <= number <= 49 for number in numbers))
            self.assertGreaterEqual(score, 0)
            self.assertLessEqual(score, 100)
            self.assertEqual(
                set(components),
                {"parity_rarity", "birthday", "adjacency", "range_spread", "decade_spread"},
            )

    def test_print_ranked_tickets(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            app.print_ranked_tickets(5, random.Random(3))
        text = output.getvalue()
        self.assertIn("LOTTO 6aus49 — anti-sharing profile — top 5 of 5 random tickets", text)
        self.assertIn("1 in 13,983,816", text)
        self.assertIn("Highest:", text)

    def test_runs_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            app.generate_ranked_tickets(0)

    def test_score_requires_valid_ticket(self) -> None:
        with self.assertRaises(ValueError):
            app.score_ticket((1, 2, 3, 4, 5))
        with self.assertRaises(ValueError):
            app.score_ticket((1, 1, 2, 3, 4, 5))


if __name__ == "__main__":
    unittest.main()
