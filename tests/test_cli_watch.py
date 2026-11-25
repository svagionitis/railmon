"""Automated tests for CLI watch subcommand (railmon watch)."""

from __future__ import annotations

import io
import json
from unittest import TestCase
from unittest.mock import MagicMock, patch

from train_tracker import TrainPosition, build_parser, run_watch_mode
from railmon_watchdog import WatchTriggerType


class TestCLIWatchParser(TestCase):
    """Test argument parsing for 'railmon watch'."""

    def setUp(self) -> None:
        self.parser = build_parser()

    def test_watch_required_argument(self) -> None:
        args = self.parser.parse_args(["watch", "IC52"])
        self.assertEqual(args.mode, "watch")
        self.assertEqual(args.train_number, "IC52")
        self.assertIsNone(args.dest)
        self.assertEqual(args.notify, "15m")
        self.assertEqual(args.distance, 15.0)
        self.assertEqual(args.delay_delta, 3.0)
        self.assertFalse(args.no_bell)
        self.assertFalse(args.no_desktop)
        self.assertFalse(args.once)
        self.assertFalse(args.json)
        self.assertFalse(args.headless)
        self.assertFalse(args.demo)

    def test_watch_custom_options(self) -> None:
        args = self.parser.parse_args([
            "watch",
            "1597",
            "--dest", "Thessaloniki",
            "--notify", "20m",
            "--distance", "25.5",
            "--delay-delta", "5.0",
            "--webhook", "https://discord.com/api/webhooks/123/xyz",
            "--webhook-format", "discord",
            "--webhook-chat-id", "456",
            "--webhook-token", "secret-token",
            "--no-bell",
            "--no-desktop",
            "--once",
            "--json",
            "--headless",
            "--demo",
        ])
        self.assertEqual(args.train_number, "1597")
        self.assertEqual(args.dest, "Thessaloniki")
        self.assertEqual(args.notify, "20m")
        self.assertEqual(args.distance, 25.5)
        self.assertEqual(args.delay_delta, 5.0)
        self.assertEqual(args.webhook, "https://discord.com/api/webhooks/123/xyz")
        self.assertEqual(args.webhook_format, "discord")
        self.assertEqual(args.webhook_chat_id, "456")
        self.assertEqual(args.webhook_token, "secret-token")
        self.assertTrue(args.no_bell)
        self.assertTrue(args.no_desktop)
        self.assertTrue(args.once)
        self.assertTrue(args.json)
        self.assertTrue(args.headless)
        self.assertTrue(args.demo)


class TestCLIWatchExecution(TestCase):
    """Test run_watch_mode execution and output formats."""

    def test_missing_train_number(self) -> None:
        mock_args = MagicMock()
        mock_args.train_number = ""
        with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            ret = run_watch_mode(mock_args)
            self.assertEqual(ret, 1)
            self.assertIn("Missing required argument", mock_err.getvalue())

    @patch("railmon_watchdog.dispatch_all_channels")
    def test_run_watch_mode_json_output(self, mock_dispatch: MagicMock) -> None:
        pos = TrainPosition(
            id="t-52",
            train_number="IC 52",
            locomotive_number="120-024",
            name="InterCity 52",
            train_type="IC",
            service_type="Passenger",
            status="Running",
            origin="Athens",
            destination="Thessaloniki",
            next_station="Oinoi",
            speed=85.0,
            course=10.0,
            track_heading=10.0,
            lat=38.3,
            lng=23.5,
            altitude=150.0,
            delay=6,
            progress=0.3,
            chainage_m=45000,
            corridor="PATHE",
            distance_to_destination_m=350000,
            timestamp="2026-11-22T14:00:00Z",
            schedule_id="sch-52",
        )

        mock_args = MagicMock()
        mock_args.train_number = "IC 52"
        mock_args.dest = "Thessaloniki"
        mock_args.notify = "15m"
        mock_args.distance = 15.0
        mock_args.delay_delta = 3.0
        mock_args.webhook = None
        mock_args.webhook_format = "auto"
        mock_args.webhook_chat_id = None
        mock_args.webhook_token = None
        mock_args.no_bell = True
        mock_args.no_desktop = True
        mock_args.once = False
        mock_args.json = True
        mock_args.headless = True
        mock_args.demo = True
        mock_args.reconnect_delay = 1.0

        def single_batch_stream(*args: object, **kwargs: object):
            yield [pos]

        with patch("train_tracker._resolve_plain_stream_source") as mock_res:
            mock_res.return_value = (single_batch_stream(), "Mock Source")
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                ret = run_watch_mode(mock_args)
                self.assertEqual(ret, 0)
                output = mock_out.getvalue().strip()
                self.assertTrue(len(output) > 0)
                first_line = output.splitlines()[0]
                record = json.loads(first_line)
                self.assertEqual(record["train_number"], "IC 52")
                self.assertEqual(record["trigger_type"], WatchTriggerType.DEPARTURE.value)

    @patch("railmon_watchdog.dispatch_all_channels")
    def test_run_watch_mode_arrival_exit_once(self, mock_dispatch: MagicMock) -> None:
        # Train arrived at Thessaloniki station
        pos = TrainPosition(
            id="t-52",
            train_number="IC 52",
            locomotive_number="120-024",
            name="InterCity 52",
            train_type="IC",
            service_type="Passenger",
            status="Running",
            origin="Athens",
            destination="Thessaloniki",
            next_station="Thessaloniki",
            speed=5.0,
            course=0.0,
            track_heading=0.0,
            lat=40.6444,
            lng=22.9298,
            altitude=10.0,
            delay=4,
            progress=1.0,
            chainage_m=500000,
            corridor="PATHE",
            distance_to_destination_m=100,
            timestamp="2026-11-22T14:00:00Z",
            schedule_id="sch-52",
        )

        mock_args = MagicMock()
        mock_args.train_number = "IC 52"
        mock_args.dest = "Thessaloniki"
        mock_args.notify = "15m"
        mock_args.distance = 15.0
        mock_args.delay_delta = 3.0
        mock_args.webhook = None
        mock_args.webhook_format = "auto"
        mock_args.webhook_chat_id = None
        mock_args.webhook_token = None
        mock_args.no_bell = True
        mock_args.no_desktop = True
        mock_args.once = True
        mock_args.json = False
        mock_args.headless = False
        mock_args.daemon = False
        mock_args.demo = True
        mock_args.reconnect_delay = 1.0

        def single_batch_stream(*args: object, **kwargs: object):
            yield [pos]

        with patch("train_tracker._resolve_plain_stream_source") as mock_res:
            mock_res.return_value = (single_batch_stream(), "Mock Source")
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                ret = run_watch_mode(mock_args)
                self.assertEqual(ret, 0)
                output = mock_out.getvalue()
                self.assertIn("Watchdog exiting (--once)", output)
