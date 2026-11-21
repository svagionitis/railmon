"""Unit tests for F-7.1: CLI Locomotive Forward Cab Video Stream Launcher (railmon cab).

Validates CLI subparser arguments, URL-only mode, structured JSON output,
offline mock engine execution, player detection overrides, and error handling.
"""

import io
import json
import unittest
from unittest.mock import MagicMock, patch

from train_tracker import build_parser, run_cab_stream_mode


class TestCLICabStream(unittest.TestCase):
    """Test suite for CLI cab video stream launcher subcommand."""

    def test_cab_subparser_arguments(self) -> None:
        """Verify cab subparser correctly parses positional train_number and flags."""
        parser = build_parser()
        args = parser.parse_args(["cab", "1597", "--url-only", "--player", "mpv", "--demo"])
        self.assertEqual(args.mode, "cab")
        self.assertEqual(args.train_number, "1597")
        self.assertTrue(args.url_only)
        self.assertEqual(args.player, "mpv")
        self.assertTrue(args.demo)

    def test_cab_subparser_alias_stream_cab(self) -> None:
        """Verify stream-cab alias works identically to cab."""
        parser = build_parser()
        args = parser.parse_args(["stream-cab", "IC52", "--json", "--demo"])
        self.assertEqual(args.mode, "stream-cab")
        self.assertEqual(args.train_number, "IC52")
        self.assertTrue(args.json)
        self.assertTrue(args.demo)

    def test_cli_cab_url_only_success(self) -> None:
        """Verify --url-only outputs raw HLS playlist URL with exit code 0."""
        parser = build_parser()
        args = parser.parse_args(["cab", "1597", "--url-only", "--demo"])

        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with patch("sys.stdout", out_buf), patch("sys.stderr", err_buf):
            ret = run_cab_stream_mode(args)

        self.assertEqual(ret, 0)
        self.assertIn("https://stream.railway.gov.gr/hls/mock-loco-460105/live.m3u8", out_buf.getvalue())
        self.assertEqual(err_buf.getvalue(), "")

    def test_cli_cab_url_only_unavailable(self) -> None:
        """Verify --url-only on an unavailable train outputs error to stderr with exit code 1."""
        parser = build_parser()
        args = parser.parse_args(["cab", "884", "--url-only", "--demo"])

        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with patch("sys.stdout", out_buf), patch("sys.stderr", err_buf):
            ret = run_cab_stream_mode(args)

        self.assertEqual(ret, 1)
        self.assertIn("No active stream for train 884", err_buf.getvalue())
        self.assertEqual(out_buf.getvalue(), "")

    def test_cli_cab_json_active(self) -> None:
        """Verify --json emits structured JSON metadata for active stream with exit code 0."""
        parser = build_parser()
        args = parser.parse_args(["cab", "1597", "--json", "--demo"])

        out_buf = io.StringIO()
        with patch("sys.stdout", out_buf):
            ret = run_cab_stream_mode(args)

        self.assertEqual(ret, 0)
        data = json.loads(out_buf.getvalue())
        self.assertEqual(data["trainNumber"], "1597")
        self.assertTrue(data["hasStream"])
        self.assertEqual(data["status"], "ONLINE")
        self.assertIn("mock-loco-460105", data["playlistUrl"])

    def test_cli_cab_json_unavailable(self) -> None:
        """Verify --json emits structured JSON metadata for unavailable stream with exit code 1."""
        parser = build_parser()
        args = parser.parse_args(["cab", "884", "--json", "--demo"])

        out_buf = io.StringIO()
        with patch("sys.stdout", out_buf):
            ret = run_cab_stream_mode(args)

        self.assertEqual(ret, 1)
        data = json.loads(out_buf.getvalue())
        self.assertEqual(data["trainNumber"], "884")
        self.assertFalse(data["hasStream"])
        self.assertEqual(data["status"], "UNAVAILABLE")

    @patch("train_tracker.launch_cab_stream")
    def test_cli_cab_interactive_launch(self, mock_launch: MagicMock) -> None:
        """Verify standard CLI command launches player and prints status message."""
        mock_launch.return_value = (True, "mpv", "Launched mpv for train 1597.")
        parser = build_parser()
        args = parser.parse_args(["cab", "1597", "--demo", "--player", "mpv"])

        out_buf = io.StringIO()
        with patch("sys.stdout", out_buf):
            ret = run_cab_stream_mode(args)

        self.assertEqual(ret, 0)
        mock_launch.assert_called_once()
        out = out_buf.getvalue()
        self.assertIn("Resolving forward cab video stream", out)
        self.assertIn("Stream Online", out)
        self.assertIn("Launched mpv for train 1597", out)

    @patch("train_tracker.launch_cab_stream")
    def test_cli_cab_browser_flag(self, mock_launch: MagicMock) -> None:
        """Verify --browser flag forwards 'browser' preferred player."""
        mock_launch.return_value = (True, "browser", "Opened stream in default web browser.")
        parser = build_parser()
        args = parser.parse_args(["cab", "1597", "--demo", "--browser"])

        out_buf = io.StringIO()
        with patch("sys.stdout", out_buf):
            ret = run_cab_stream_mode(args)

        self.assertEqual(ret, 0)
        _, kwargs = mock_launch.call_args
        self.assertEqual(kwargs.get("preferred_player"), "browser")


if __name__ == "__main__":
    unittest.main()
