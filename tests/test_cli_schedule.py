"""Unit tests for F-6.2: CLI Train Timetable & Intermediate Stop Inspector (railmon schedule).

Validates train number normalization, mock timetable synthesis, UTF-8 table formatting,
7-bit ASCII purity, structured JSON output, "Did you mean" suggestions on miss, and network resilience.
"""

import io
import json
import unittest
from unittest.mock import MagicMock, patch

from storage.cli_formatters import format_train_schedule_table
from train_tracker import RailwayAPIError, build_parser, main, run_schedule_mode
from tui.mock_engine import (
    generate_mock_train_schedule,
    get_active_mock_train_numbers,
    load_stations,
)


class TestCLISchedule(unittest.TestCase):
    """Test suite for CLI train schedule and timetable inspector."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.stations = load_stations()

    def test_tc_sch_01_cli_subparser_arguments(self) -> None:
        """TC-SCH-01: Verify schedule subparser parses positional and flag arguments."""
        parser = build_parser()
        args = parser.parse_args(["schedule", "1597", "--demo", "--json", "--ascii"])
        self.assertEqual(args.mode, "schedule")
        self.assertEqual(args.train_number, "1597")
        self.assertTrue(args.demo)
        self.assertTrue(args.json)
        self.assertTrue(args.ascii)

    def test_tc_sch_02_mock_schedule_generation(self) -> None:
        """TC-SCH-02: Verify generate_mock_train_schedule generates realistic stops for corridors."""
        # 1. Proastiakos Chalkida (1597)
        sched_1597 = generate_mock_train_schedule("1597", self.stations)
        self.assertEqual(sched_1597["trainNumber"], "1597")
        self.assertEqual(sched_1597["trainType"], "Proastiakos")
        self.assertGreater(len(sched_1597["stops"]), 5)
        # Check origin and destination
        self.assertEqual(sched_1597["origin"]["id"], "ATH")
        self.assertEqual(sched_1597["destination"]["id"], "CHAL")
        # Check stop sequence numbering
        seqs = [s["stopSequence"] for s in sched_1597["stops"]]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))

        # 2. Intercity PATHE (IC 52)
        sched_ic52 = generate_mock_train_schedule("IC 52", self.stations)
        self.assertEqual(sched_ic52["trainNumber"], "IC 52")
        self.assertEqual(sched_ic52["trainType"], "IC")
        self.assertEqual(sched_ic52["origin"]["id"], "ATH")
        self.assertEqual(sched_ic52["destination"]["id"], "SKG")
        self.assertGreater(len(sched_ic52["stops"]), 8)

        # 3. Airport - Kiato (2232)
        sched_2232 = generate_mock_train_schedule("2232", self.stations)
        self.assertEqual(sched_2232["origin"]["id"], "AIA")
        self.assertEqual(sched_2232["destination"]["id"], "KIA")

    def test_tc_sch_03_format_train_schedule_table_utf8(self) -> None:
        """TC-SCH-03: Verify format_train_schedule_table renders table with bilingual station names."""
        timetable = generate_mock_train_schedule("1597", self.stations)
        output = format_train_schedule_table(timetable, is_ascii=False)

        self.assertIn("TRAIN TIMETABLE & ITINERARY: TRAIN 1597", output)
        self.assertIn("ST   STATION", output)
        self.assertIn("SCHED ARR", output)
        self.assertIn("ACTUAL ARR", output)
        self.assertIn("SCHED DEP", output)
        self.assertIn("ACTUAL DEP", output)
        self.assertIn("PLAT", output)
        self.assertIn("STATUS", output)
        self.assertIn("DELAY", output)
        self.assertIn("Total stops:", output)

        # Confirm station names present
        self.assertIn("Athens", output)
        self.assertIn("Chalkida", output)

    def test_tc_sch_04_format_train_schedule_table_ascii_purity(self) -> None:
        """TC-SCH-04: Verify format_train_schedule_table with is_ascii=True enforces pure 7-bit ASCII."""
        timetable = generate_mock_train_schedule("1597", self.stations)
        output = format_train_schedule_table(timetable, is_ascii=True)

        for line_num, line in enumerate(output.splitlines(), start=1):
            for ch in line:
                self.assertLess(
                    ord(ch),
                    128,
                    f"Non-ASCII character {ch!r} (ord={ord(ch)}) at line {line_num}: {line}",
                )

        self.assertIn("->", output)
        self.assertNotIn("→", output)
        self.assertNotIn("►", output)

    def test_tc_sch_05_cli_schedule_json_output(self) -> None:
        """TC-SCH-05: Verify --json outputs valid JSON matching timetable schema."""
        args = build_parser().parse_args(["schedule", "1597", "--demo", "--json"])

        stdout_capture = io.StringIO()
        with patch("sys.stdout", stdout_capture):
            code = run_schedule_mode(args)

        self.assertEqual(code, 0)
        parsed = json.loads(stdout_capture.getvalue())
        self.assertEqual(parsed["trainNumber"], "1597")
        self.assertIn("stops", parsed)
        self.assertIsInstance(parsed["stops"], list)
        self.assertGreater(len(parsed["stops"]), 0)
        first_stop = parsed["stops"][0]
        self.assertIn("stationId", first_stop)
        self.assertIn("scheduledDeparture", first_stop)

    def test_tc_sch_06_unfound_train_suggestions(self) -> None:
        """TC-SCH-06: Verify unfound train queries print suggestions and return exit code 1."""
        args = build_parser().parse_args(["schedule", "159"])

        stderr_capture = io.StringIO()
        with patch.object(
            RailwayAPIError,
            "__init__",
            lambda self, code, msg, url=None: None,
        ), patch("train_tracker.RailwayGovClient.get_schedule_by_train_number", side_effect=RailwayAPIError(404, "Not Found")), patch("sys.stderr", stderr_capture):
            code = run_schedule_mode(args)

        self.assertEqual(code, 1)
        err_out = stderr_capture.getvalue()
        self.assertIn("Failed to fetch schedule for train '159'", err_out)
        self.assertIn("Did you mean:", err_out)
        self.assertIn("1597", err_out)

    def test_tc_sch_07_network_error_resilience(self) -> None:
        """TC-SCH-07: Verify network errors exit gracefully with code 1 and no traceback."""
        args = build_parser().parse_args(["schedule", "IC52"])

        stderr_capture = io.StringIO()
        with patch("train_tracker.RailwayGovClient.get_schedule_by_train_number", side_effect=TimeoutError("Connection timed out")), patch("sys.stderr", stderr_capture):
            code = run_schedule_mode(args)

        self.assertEqual(code, 1)
        err_out = stderr_capture.getvalue()
        self.assertIn("Connection timed out", err_out)

    def test_tc_sch_08_empty_stops_handling(self) -> None:
        """TC-SCH-08: Verify empty timetable stops list handles gracefully."""
        empty_timetable = {
            "trainNumber": "9999",
            "origin": {"nameEnglish": "TestA"},
            "destination": {"nameEnglish": "TestB"},
            "stops": [],
        }
        output = format_train_schedule_table(empty_timetable)
        self.assertIn("No intermediate calling points or timetable records found.", output)

    def test_tc_sch_09_active_mock_train_numbers_list(self) -> None:
        """TC-SCH-09: Verify get_active_mock_train_numbers returns non-empty list of trains."""
        trains = get_active_mock_train_numbers()
        self.assertIn("1597", trains)
        self.assertIn("IC 52", trains)
        self.assertIn("2232", trains)


if __name__ == "__main__":
    unittest.main()
