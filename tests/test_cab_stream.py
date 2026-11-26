"""Unit tests for F-7.1: Live Locomotive Forward Cab Video Stream Launcher.

Tests CabStreamInfo DTO parsing, URL validation, train identifier sanitization,
video player discovery and prioritization, command building, and detached process execution.
"""

import os
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from train_tracker import (
    CabStreamInfo,
    build_player_command,
    detect_video_player,
    launch_cab_stream,
    launch_detached_player,
    sanitize_train_identifier,
    validate_stream_url,
)


class TestCabStreamInfo(unittest.TestCase):
    """Test CabStreamInfo serialization, parsing, and factory methods."""

    def test_from_dict_active_stream(self) -> None:
        payload = {
            "trainNumber": "1597",
            "locomotiveId": "460-105",
            "hasStream": True,
            "status": "ONLINE",
            "cameraPosition": "FORWARD_CAB",
            "streamType": "hls",
            "playlistUrl": "https://stream.railway.gov.gr/hls/loco-460105/live.m3u8",
            "resolution": "1080p",
            "fps": 30,
            "bitrateKbps": 4500,
            "latencySec": 1.8,
        }
        info = CabStreamInfo.from_dict(payload)
        self.assertEqual(info.train_number, "1597")
        self.assertTrue(info.has_stream)
        self.assertEqual(info.status, "ONLINE")
        self.assertEqual(info.stream_type, "hls")
        self.assertEqual(info.playlist_url, "https://stream.railway.gov.gr/hls/loco-460105/live.m3u8")
        self.assertEqual(info.locomotive_id, "460-105")
        self.assertEqual(info.resolution, "1080p")
        self.assertEqual(info.fps, 30)
        self.assertEqual(info.bitrate_kbps, 4500)
        self.assertAlmostEqual(info.latency_sec or 0.0, 1.8)

    def test_from_dict_unavailable_stream(self) -> None:
        payload = {
            "trainNumber": "884",
            "hasStream": False,
            "status": "UNAVAILABLE",
            "message": "Locomotive not equipped with camera.",
        }
        info = CabStreamInfo.from_dict(payload)
        self.assertEqual(info.train_number, "884")
        self.assertFalse(info.has_stream)
        self.assertEqual(info.status, "UNAVAILABLE")
        self.assertIsNone(info.playlist_url)
        self.assertEqual(info.error_message, "Locomotive not equipped with camera.")

    def test_unavailable_factory(self) -> None:
        info = CabStreamInfo.unavailable("3570", "No video stream.")
        self.assertEqual(info.train_number, "3570")
        self.assertFalse(info.has_stream)
        self.assertEqual(info.status, "UNAVAILABLE")
        self.assertEqual(info.error_message, "No video stream.")

    def test_to_dict_roundtrip(self) -> None:
        info = CabStreamInfo(
            train_number="IC52",
            has_stream=True,
            status="ONLINE",
            playlist_url="https://example.com/live.m3u8",
            resolution="720p",
        )
        d = info.to_dict()
        self.assertEqual(d["trainNumber"], "IC52")
        self.assertTrue(d["hasStream"])
        self.assertEqual(d["playlistUrl"], "https://example.com/live.m3u8")


class TestStreamValidationAndSanitization(unittest.TestCase):
    """Test URL validation and train identifier sanitization."""

    def test_validate_stream_url_valid_http_and_https(self) -> None:
        u1 = "https://stream.railway.gov.gr/hls/live.m3u8"
        self.assertEqual(validate_stream_url(u1), u1)
        u2 = "http://localhost:8080/stream.m3u8"
        self.assertEqual(validate_stream_url(u2), u2)

    def test_validate_stream_url_insecure_scheme_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_stream_url("file:///etc/passwd")
        with self.assertRaises(ValueError):
            validate_stream_url("javascript:alert(1)")
        with self.assertRaises(ValueError):
            validate_stream_url("ftp://example.com/file.m3u8")

    def test_validate_stream_url_missing_hostname(self) -> None:
        with self.assertRaises(ValueError):
            validate_stream_url("http://")

    def test_validate_stream_url_empty(self) -> None:
        with self.assertRaises(ValueError):
            validate_stream_url("")

    def test_sanitize_train_identifier_valid(self) -> None:
        self.assertEqual(sanitize_train_identifier("1597"), "1597")
        self.assertEqual(sanitize_train_identifier("IC 52"), "IC 52")
        self.assertEqual(sanitize_train_identifier("ETR-470"), "ETR-470")

    def test_sanitize_train_identifier_injection_rejected(self) -> None:
        with self.assertRaises(ValueError):
            sanitize_train_identifier("1597; rm -rf /")
        with self.assertRaises(ValueError):
            sanitize_train_identifier("IC52 | bash")
        with self.assertRaises(ValueError):
            sanitize_train_identifier("")


class TestPlayerDiscoveryAndCommandBuilding(unittest.TestCase):
    """Test media player discovery hierarchy and command construction."""

    @patch("shutil.which")
    def test_detect_video_player_prefers_mpv(self, mock_which: MagicMock) -> None:
        mock_which.side_effect = lambda cmd: "/usr/bin/mpv" if cmd == "mpv" else None
        player, path = detect_video_player()
        self.assertEqual(player, "mpv")
        self.assertEqual(path, "/usr/bin/mpv")

    @patch("shutil.which")
    def test_detect_video_player_falls_back_to_ffplay(self, mock_which: MagicMock) -> None:
        mock_which.side_effect = lambda cmd: "/usr/bin/ffplay" if cmd == "ffplay" else None
        player, path = detect_video_player()
        self.assertEqual(player, "ffplay")
        self.assertEqual(path, "/usr/bin/ffplay")

    @patch("shutil.which")
    def test_detect_video_player_falls_back_to_vlc(self, mock_which: MagicMock) -> None:
        mock_which.side_effect = lambda cmd: "/usr/bin/vlc" if cmd == "vlc" else None
        player, path = detect_video_player()
        self.assertEqual(player, "vlc")
        self.assertEqual(path, "/usr/bin/vlc")

    @patch("shutil.which")
    def test_detect_video_player_falls_back_to_browser(self, mock_which: MagicMock) -> None:
        mock_which.return_value = None
        player, path = detect_video_player()
        self.assertEqual(player, "browser")
        self.assertIsNone(path)

    @patch("shutil.which")
    def test_detect_video_player_with_preferred_override(self, mock_which: MagicMock) -> None:
        mock_which.side_effect = lambda cmd: "/opt/custom/player" if cmd == "custom" else None
        player, path = detect_video_player(preferred_player="custom")
        self.assertEqual(player, "custom")
        self.assertEqual(path, "/opt/custom/player")

    def test_detect_video_player_with_browser_override(self) -> None:
        player, path = detect_video_player(preferred_player="browser")
        self.assertEqual(player, "browser")
        self.assertIsNone(path)

    def test_build_player_command_mpv(self) -> None:
        cmd = build_player_command(
            "mpv",
            "/usr/bin/mpv",
            "https://stream.railway.gov.gr/hls/live.m3u8",
            "1597",
        )
        self.assertEqual(cmd[0], "/usr/bin/mpv")
        self.assertIn("--title=railmon: 1597 Forward Cab View", cmd)
        self.assertIn("--no-terminal", cmd)
        self.assertIn("https://stream.railway.gov.gr/hls/live.m3u8", cmd)

    def test_build_player_command_ffplay(self) -> None:
        cmd = build_player_command(
            "ffplay",
            "/usr/bin/ffplay",
            "https://stream.railway.gov.gr/hls/live.m3u8",
            "IC52",
        )
        self.assertEqual(cmd[0], "/usr/bin/ffplay")
        self.assertIn("-window_title", cmd)
        self.assertIn("railmon: IC52 Forward Cab View", cmd)
        self.assertIn("-autoexit", cmd)

    def test_build_player_command_vlc(self) -> None:
        cmd = build_player_command(
            "vlc",
            "/usr/bin/vlc",
            "https://stream.railway.gov.gr/hls/live.m3u8",
            "2232",
        )
        self.assertEqual(cmd[0], "/usr/bin/vlc")
        self.assertIn("--meta-title=railmon: 2232 Forward Cab View", cmd)
        self.assertIn("--play-and-exit", cmd)


class TestPlayerExecution(unittest.TestCase):
    """Test launch_detached_player and launch_cab_stream."""

    @patch("subprocess.Popen")
    def test_launch_detached_player_posix(self, mock_popen: MagicMock) -> None:
        launch_detached_player(["mpv", "https://example.com/live.m3u8"], is_windows=False)
        mock_popen.assert_called_once()
        _, kwargs = mock_popen.call_args
        self.assertTrue(kwargs.get("start_new_session"))
        self.assertEqual(kwargs.get("stdin"), subprocess.DEVNULL)
        self.assertEqual(kwargs.get("stdout"), subprocess.DEVNULL)
        self.assertEqual(kwargs.get("stderr"), subprocess.DEVNULL)

    @patch("subprocess.Popen")
    def test_launch_detached_player_windows(self, mock_popen: MagicMock) -> None:
        launch_detached_player(["mpv.exe", "https://example.com/live.m3u8"], is_windows=True)
        mock_popen.assert_called_once()
        _, kwargs = mock_popen.call_args
        self.assertIn("creationflags", kwargs)
        self.assertEqual(kwargs.get("stdin"), subprocess.DEVNULL)

    @patch("train_tracker.launch_detached_player")
    @patch("train_tracker.detect_video_player")
    def test_launch_cab_stream_success_player(
        self,
        mock_detect: MagicMock,
        mock_launch: MagicMock,
    ) -> None:
        mock_detect.return_value = ("mpv", "/usr/bin/mpv")
        info = CabStreamInfo(
            train_number="1597",
            has_stream=True,
            status="ONLINE",
            playlist_url="https://stream.railway.gov.gr/hls/live.m3u8",
        )
        success, player, msg = launch_cab_stream(info)
        self.assertTrue(success)
        self.assertEqual(player, "mpv")
        self.assertIn("Launched mpv", msg)
        mock_launch.assert_called_once()

    @patch("webbrowser.open")
    @patch("train_tracker.detect_video_player")
    def test_launch_cab_stream_browser_fallback(
        self,
        mock_detect: MagicMock,
        mock_browser: MagicMock,
    ) -> None:
        mock_detect.return_value = ("browser", None)
        mock_browser.return_value = True
        info = CabStreamInfo(
            train_number="1597",
            has_stream=True,
            status="ONLINE",
            playlist_url="https://stream.railway.gov.gr/hls/live.m3u8",
        )
        success, player, msg = launch_cab_stream(info)
        self.assertTrue(success)
        self.assertEqual(player, "browser")
        self.assertIn("web browser", msg)
        mock_browser.assert_called_once_with("https://stream.railway.gov.gr/hls/live.m3u8")

    def test_launch_cab_stream_unavailable(self) -> None:
        info = CabStreamInfo.unavailable("884", "No camera installed.")
        success, player, msg = launch_cab_stream(info)
        self.assertFalse(success)
        self.assertEqual(player, "none")
        self.assertIn("No camera installed", msg)

    def test_cab_url_only_validation_and_player_option_delimiters(self) -> None:
        """TC-SEC-06: --url-only validates stream URL; player commands include '--' / '-i' delimiters."""
        import argparse
        import io
        from contextlib import redirect_stderr
        from train_tracker import run_cab_stream_mode

        url = "https://stream.railway.gov.gr/hls/live.m3u8"
        mpv_cmd = build_player_command("mpv", "/usr/bin/mpv", url, "IC52")
        self.assertEqual(mpv_cmd[-2:], ["--", url])

        vlc_cmd = build_player_command("vlc", "/usr/bin/vlc", url, "IC52")
        self.assertEqual(vlc_cmd[-2:], ["--", url])

        ffplay_cmd = build_player_command("ffplay", "/usr/bin/ffplay", url, "IC52")
        self.assertEqual(ffplay_cmd[-2:], ["-i", url])

        bad_info = CabStreamInfo(
            train_number="IC52",
            has_stream=True,
            status="ONLINE",
            playlist_url="javascript:alert(1)",
        )
        args = argparse.Namespace(
            train_number="IC52",
            demo=False,
            player=None,
            url_only=True,
            browser=False,
            json=False,
            ascii=True,
        )
        err_buf = io.StringIO()
        with patch("train_tracker.RailwayGovClient.get_cab_stream_url", return_value=bad_info), \
             redirect_stderr(err_buf):
            rc = run_cab_stream_mode(args)
        self.assertEqual(rc, 1)
        self.assertIn("Invalid stream URL", err_buf.getvalue())


if __name__ == "__main__":
    unittest.main()
