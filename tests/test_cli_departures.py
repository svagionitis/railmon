"""Unit tests for F-6.1: CLI Station Departure and Arrival Boards (railmon departures).

Validates station resolution, diacritic stripping, suggestions on miss, table formatting,
arrivals toggling, service type filtering, JSON serialization, and error resilience.
"""

import io
import json
import unittest
from unittest.mock import MagicMock, patch

from storage.cli_formatters import format_station_board_table
from train_tracker import RailwayAPIError, build_parser, main, run_departures_mode
from tui.mock_engine import (
    StationNode,
    generate_mock_station_board,
    load_stations,
    normalize_station_key,
    resolve_station_for_cli,
)


class TestCLIDepartures(unittest.TestCase):
    """Test suite for CLI departures and arrival boards."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.stations = load_stations()

    def test_tc_dep_01_station_resolution_diacritics_and_aliases(self) -> None:
        """TC-DEP-01: Verify station resolution across Greek, Latin, codes, and diacritics."""
        queries = ["ATH", "ath", "1001", "Αθήνα", "αθηνα", "ΑΘΗΝΑ", "Athens", "Σ.Σ. Λαρίσης"]
        for q in queries:
            stn, suggestions = resolve_station_for_cli(q, self.stations)
            self.assertIsNotNone(stn, f"Failed to resolve station for query: {q}")
            assert stn is not None
            self.assertEqual(stn.id, "ATH", f"Query '{q}' resolved to wrong station {stn.id}")
            self.assertEqual(len(suggestions), 0)

        # Test SKA
        ska_queries = ["SKA", "ska", "1005", "ΣΚΑ", "Acharnes", "Σιδηροδρομικό Κέντρο Αχαρνών"]
        for q in ska_queries:
            stn, _ = resolve_station_for_cli(q, self.stations)
            self.assertIsNotNone(stn, f"Failed to resolve SKA for query: {q}")
            assert stn is not None
            self.assertEqual(stn.id, "SKA")

    def test_tc_dep_02_unknown_station_suggestions(self) -> None:
        """TC-DEP-02: Verify unknown station queries return suggestions."""
        stn, suggestions = resolve_station_for_cli("Thessalonk", self.stations)
        self.assertIsNone(stn)
        self.assertGreater(len(suggestions), 0)
        has_thessaloniki = any("Thessaloniki" in s for s in suggestions)
        self.assertTrue(has_thessaloniki, f"Expected 'Thessaloniki' in suggestions: {suggestions}")

        # Empty query
        stn_empty, sugg_empty = resolve_station_for_cli("", self.stations)
        self.assertIsNone(stn_empty)
        self.assertEqual(sugg_empty, [])

    def test_tc_dep_03_table_formatting_departures(self) -> None:
        """TC-DEP-03: Verify departures table formatting in UTF-8."""
        station = self.stations["ATH"]
        records = [
            {
                "trainNumber": "IC52",
                "serviceType": "Intercity",
                "scheduledTime": "14:32",
                "destination": {"nameGreek": "Θεσσαλονίκη", "nameEnglish": "Thessaloniki"},
                "platform": "1",
                "delayMinutes": 0,
                "status": "ON_TIME",
            },
            {
                "trainNumber": "1597",
                "serviceType": "Suburban",
                "scheduledTime": "14:45",
                "destination": {"nameGreek": "Χαλκίδα", "nameEnglish": "Chalkida"},
                "platform": "3",
                "delayMinutes": 8,
                "status": "DELAYED",
            },
        ]
        table = format_station_board_table(station, records, is_arrivals=False, is_ascii=False)
        self.assertIn("STATION DEPARTURE BOARD", table)
        self.assertIn("DESTINATION", table)
        self.assertIn("IC52", table)
        self.assertIn("1597", table)
        self.assertIn("ON TIME", table)
        self.assertIn("DELAYED", table)
        self.assertIn("+8m", table)

    def test_tc_dep_04_table_formatting_arrivals(self) -> None:
        """TC-DEP-04: Verify arrivals table formatting flips destination to origin."""
        station = self.stations["SKG"]
        records = [
            {
                "trainNumber": "IC53",
                "serviceType": "Intercity",
                "scheduledTime": "18:20",
                "origin": {"nameGreek": "Αθήνα", "nameEnglish": "Athens"},
                "platform": "2",
                "delayMinutes": 0,
                "status": "ON_TIME",
            }
        ]
        table = format_station_board_table(station, records, is_arrivals=True, is_ascii=False)
        self.assertIn("STATION ARRIVAL BOARD", table)
        self.assertIn("ORIGIN", table)
        self.assertIn("Athens", table)
        self.assertIn("Total arrivals: 1", table)

    def test_tc_dep_05_pure_ascii_fallback(self) -> None:
        """TC-DEP-05: Verify --ascii mode outputs zero non-ASCII characters."""
        station = self.stations["ATH"]
        records = generate_mock_station_board(station, is_arrivals=False, limit=5)
        table = format_station_board_table(station, records, is_arrivals=False, is_ascii=True)
        for ch in table:
            self.assertLess(ord(ch), 128, f"Non-ASCII character found: '{ch}' (U+{ord(ch):04X})")

    def test_tc_dep_06_json_output(self) -> None:
        """TC-DEP-06: Verify --json outputs clean, valid JSON matching schema."""
        parser = build_parser()
        args = parser.parse_args(["departures", "Athens", "--demo", "--json", "--limit", "3"])
        with patch("sys.stdout", new=io.StringIO()) as fake_out:
            code = run_departures_mode(args)
            self.assertEqual(code, 0)
            output = fake_out.getvalue()
            data = json.loads(output)
            self.assertEqual(data["station"]["id"], "ATH")
            self.assertEqual(data["mode"], "departures")
            self.assertEqual(data["count"], 3)
            self.assertEqual(len(data["records"]), 3)

    def test_tc_dep_07_service_type_filtering(self) -> None:
        """TC-DEP-07: Verify filtering by service type."""
        parser = build_parser()
        args = parser.parse_args(["departures", "Athens", "--demo", "--type", "intercity"])
        with patch("sys.stdout", new=io.StringIO()) as fake_out:
            code = run_departures_mode(args)
            self.assertEqual(code, 0)
            output = fake_out.getvalue()
            self.assertIn("IC52", output)
            self.assertIn("IC54", output)
            self.assertNotIn("1597", output)  # Suburban train filtered out

    def test_tc_dep_08_rate_limit_and_network_resilience(self) -> None:
        """TC-DEP-08: Verify graceful handling of API errors (429 and network failures)."""
        parser = build_parser()
        args = parser.parse_args(["departures", "Athens"])

        with patch("train_tracker.RailwayGovClient.get_station_departures") as mock_get:
            mock_get.side_effect = RailwayAPIError("HTTP 429 Rate Limited")
            with patch("sys.stderr", new=io.StringIO()) as fake_err:
                code = run_departures_mode(args)
                self.assertEqual(code, 1)
                err_msg = fake_err.getvalue()
                self.assertIn("[Error] Failed to fetch station timetable", err_msg)
                self.assertIn("429", err_msg)

    def test_tc_dep_09_offline_demo_mode_end_to_end(self) -> None:
        """TC-DEP-09: Verify end-to-end departures execution in --demo mode via main()."""
        with patch("sys.stdout", new=io.StringIO()) as fake_out:
            code = main(["departures", "SKA", "--demo", "--limit", "4"])
            self.assertEqual(code, 0)
            output = fake_out.getvalue()
            self.assertIn("STATION DEPARTURE BOARD", output)
            self.assertIn("SKA", output)
            self.assertIn("Total departures: 4", output)

    def test_tc_dep_10_station_not_found_cli_exit(self) -> None:
        """TC-DEP-10: Verify unknown station prints error and suggestions to stderr with code 1."""
        with patch("sys.stderr", new=io.StringIO()) as fake_err:
            code = main(["departures", "NonExistentStationXYZ123"])
            self.assertEqual(code, 1)
            err_msg = fake_err.getvalue()
            self.assertIn("[Error] Station not found: 'NonExistentStationXYZ123'", err_msg)
            self.assertIn("Did you mean:", err_msg)

    def test_tc_dep_11_empty_records_table(self) -> None:
        """TC-DEP-11: Verify table formatting when records list is empty."""
        station = self.stations["ATH"]
        table = format_station_board_table(station, [], is_arrivals=False)
        self.assertIn("No upcoming departures scheduled.", table)

        arr_table = format_station_board_table(station, [], is_arrivals=True)
        self.assertIn("No upcoming arrivals scheduled.", arr_table)


if __name__ == "__main__":
    unittest.main()
