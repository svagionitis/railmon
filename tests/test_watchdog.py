"""Unit tests for watchdog.py (F-6.3 Proximity & Delay Push Alerting Engine)."""

from __future__ import annotations

import json
from unittest import TestCase
from unittest.mock import MagicMock, patch

from tui.geo import GeoPoint
from watchdog import (
    NotificationChannel,
    WatchAlertEvent,
    WatchConfig,
    WatchState,
    WatchTriggerType,
    WatchdogEvaluator,
    WebhookFormat,
    dispatch_all_channels,
    dispatch_desktop_notification,
    dispatch_terminal_bell,
    dispatch_webhook,
    format_watch_event_line,
    format_watch_ticker,
    matches_train_number,
    normalize_train_query,
    parse_notify_minutes,
    resolve_destination_station,
)


class DummyPosition:
    """Mock TrainPosition for watchdog testing."""

    def __init__(
        self,
        train_number: str = "IC 52",
        id: str = "t-52",
        speed: float = 0.0,
        delay: float = 0.0,
        lat: float = 38.0,
        lng: float = 23.7,
        origin: str = "Athens",
        destination: str = "Thessaloniki",
        next_station: str = "Oinoi",
    ) -> None:
        self.train_number = train_number
        self.id = id
        self.speed = speed
        self.delay = delay
        self.lat = lat
        self.lng = lng
        self.origin = origin
        self.destination = destination
        self.next_station = next_station


class TestWatchdogMatchingAndHelpers(TestCase):
    """Test train matching, normalization, and parsing helper utilities."""

    def test_normalize_train_query(self) -> None:
        self.assertEqual(normalize_train_query("IC 52"), "IC52")
        self.assertEqual(normalize_train_query("1597-A"), "1597A")
        self.assertEqual(normalize_train_query("  ic_52  "), "IC52")

    def test_matches_train_number(self) -> None:
        self.assertTrue(matches_train_number("IC 52", "t-52", "IC52"))
        self.assertTrue(matches_train_number("1597", "1597", "1597"))
        self.assertTrue(matches_train_number("IC52", "t-52", "52"))
        self.assertTrue(matches_train_number("52", "t-52", "IC52"))
        self.assertTrue(matches_train_number(None, "IC-52", "IC 52"))
        self.assertFalse(matches_train_number("884", "t-884", "IC52"))
        self.assertFalse(matches_train_number("", "", "1597"))

    def test_parse_notify_minutes(self) -> None:
        self.assertEqual(parse_notify_minutes("15m"), 15.0)
        self.assertEqual(parse_notify_minutes("20 min"), 20.0)
        self.assertEqual(parse_notify_minutes("10"), 10.0)
        self.assertEqual(parse_notify_minutes(30), 30.0)
        self.assertEqual(parse_notify_minutes("invalid"), 15.0)

    def test_resolve_destination_station(self) -> None:
        stn, name = resolve_destination_station("Thessaloniki")
        if stn is not None:
            self.assertIn("Thessaloniki", str(name) + str(stn.name_en) + str(stn.name_el))

        stn_empty, name_empty = resolve_destination_station(None)
        self.assertIsNone(stn_empty)
        self.assertIsNone(name_empty)


class TestWatchdogEvaluatorTriggers(TestCase):
    """Test trigger evaluations: departure, delay increase/decrease, proximity, arrival."""

    def setUp(self) -> None:
        # Athens Station ~ 37.9922, 23.7208
        # Thessaloniki Station ~ 40.6444, 22.9298
        self.dest_point = GeoPoint(lat=40.6444, lng=22.9298)
        self.config = WatchConfig(
            train_number="IC 52",
            destination_station="Thessaloniki",
            destination_point=self.dest_point,
            destination_name="Thessaloniki",
            notify_eta_minutes=15.0,
            notify_distance_km=15.0,
            delay_delta_threshold_min=3.0,
            cooldown_delay_alert_sec=60.0,
            telemetry_timeout_sec=30.0,
        )
        self.evaluator = WatchdogEvaluator(self.config)

    def test_departure_trigger(self) -> None:
        # Initial frame stationary at Athens
        pos1 = DummyPosition(train_number="IC 52", speed=0.0, lat=37.9922, lng=23.7208)
        events1 = self.evaluator.evaluate_position(pos1, now=1000.0)
        self.assertEqual(len(events1), 0)
        self.assertFalse(self.evaluator.state.has_departed)

        # Second frame moving at 45 km/h
        pos2 = DummyPosition(train_number="IC 52", speed=45.0, lat=38.0100, lng=23.7300)
        events2 = self.evaluator.evaluate_position(pos2, now=1010.0)
        self.assertEqual(len(events2), 1)
        self.assertEqual(events2[0].trigger_type, WatchTriggerType.DEPARTURE)
        self.assertTrue(self.evaluator.state.has_departed)

        # Subsequent moving frame should not fire departure again
        pos3 = DummyPosition(train_number="IC 52", speed=80.0, lat=38.0500, lng=23.7400)
        events3 = self.evaluator.evaluate_position(pos3, now=1020.0)
        dep_events = [e for e in events3 if e.trigger_type == WatchTriggerType.DEPARTURE]
        self.assertEqual(len(dep_events), 0)

    def test_delay_increase_and_cooldown(self) -> None:
        # Frame 1: baseline delay 0m
        pos1 = DummyPosition(speed=60.0, delay=0.0, lat=38.5, lng=23.0)
        self.evaluator.evaluate_position(pos1, now=1000.0)

        # Frame 2: delay jumps to +5m (delta +5m >= threshold 3m)
        pos2 = DummyPosition(speed=60.0, delay=5.0, lat=38.6, lng=23.0)
        events2 = self.evaluator.evaluate_position(pos2, now=1010.0)
        delay_events = [e for e in events2 if e.trigger_type == WatchTriggerType.DELAY_INCREASE]
        self.assertEqual(len(delay_events), 1)
        self.assertEqual(delay_events[0].delay_delta_min, 5.0)

        # Frame 3: delay increases again to +7m (delta +2m < threshold 3m) within cooldown
        pos3 = DummyPosition(speed=60.0, delay=7.0, lat=38.7, lng=23.0)
        events3 = self.evaluator.evaluate_position(pos3, now=1020.0)
        delay_events3 = [e for e in events3 if e.trigger_type == WatchTriggerType.DELAY_INCREASE]
        self.assertEqual(len(delay_events3), 0)

        # Frame 4: after cooldown expires (now=1100 > 1010 + 60), delay jumps to +11m (delta +6m >= 3m)
        pos4 = DummyPosition(speed=60.0, delay=11.0, lat=38.8, lng=23.0)
        events4 = self.evaluator.evaluate_position(pos4, now=1100.0)
        delay_events4 = [e for e in events4 if e.trigger_type == WatchTriggerType.DELAY_INCREASE]
        self.assertEqual(len(delay_events4), 1)

    def test_delay_reduction_recovery(self) -> None:
        # Frame 1: delay is 15m
        pos1 = DummyPosition(speed=60.0, delay=15.0, lat=38.5, lng=23.0)
        self.evaluator.evaluate_position(pos1, now=1000.0)

        # Frame 2: delay drops to 10m (delta -5m <= -threshold 3m)
        pos2 = DummyPosition(speed=60.0, delay=10.0, lat=38.6, lng=23.0)
        events2 = self.evaluator.evaluate_position(pos2, now=1080.0)
        reduced = [e for e in events2 if e.trigger_type == WatchTriggerType.DELAY_REDUCED]
        self.assertEqual(len(reduced), 1)
        self.assertEqual(reduced[0].delay_delta_min, -5.0)

    def test_proximity_distance_and_eta_latching(self) -> None:
        # Establish departed train
        pos0 = DummyPosition(speed=80.0, lat=39.0, lng=22.5)
        self.evaluator.evaluate_position(pos0, now=1000.0)

        # Place train 12 km from Thessaloniki (within 15km threshold)
        # Thessaloniki is (40.6444, 22.9298)
        # ~0.1 deg lat is ~11 km
        pos_near = DummyPosition(speed=80.0, lat=40.5400, lng=22.9298)
        events = self.evaluator.evaluate_position(pos_near, now=1020.0)

        prox_dist = [e for e in events if e.trigger_type == WatchTriggerType.PROXIMITY_DISTANCE]
        self.assertEqual(len(prox_dist), 1)
        self.assertLessEqual(prox_dist[0].distance_to_dest_km, 15.0)
        self.assertTrue(self.evaluator.state.distance_alert_fired)

        # Next frame still near destination should NOT re-fire distance alert
        pos_near2 = DummyPosition(speed=75.0, lat=40.5600, lng=22.9298)
        events2 = self.evaluator.evaluate_position(pos_near2, now=1030.0)
        prox_dist2 = [e for e in events2 if e.trigger_type == WatchTriggerType.PROXIMITY_DISTANCE]
        self.assertEqual(len(prox_dist2), 0)

    def test_arrival_detection(self) -> None:
        # Place train directly at Thessaloniki Station within 300m, slow speed 10 km/h
        pos_arr = DummyPosition(speed=10.0, lat=40.6450, lng=22.9300)
        events = self.evaluator.evaluate_position(pos_arr, now=1050.0)

        arr = [e for e in events if e.trigger_type == WatchTriggerType.ARRIVAL]
        self.assertEqual(len(arr), 1)
        self.assertTrue(self.evaluator.state.arrival_alert_fired)
        self.assertIn("Thessaloniki", arr[0].headline)

    def test_telemetry_timeout(self) -> None:
        pos1 = DummyPosition(speed=50.0, lat=39.0, lng=22.0)
        self.evaluator.evaluate_position(pos1, now=1000.0)

        # Advance 40 seconds (timeout is 30s)
        lost_event = self.evaluator.evaluate_timeout(now=1040.0)
        self.assertIsNotNone(lost_event)
        self.assertEqual(lost_event.trigger_type, WatchTriggerType.TELEMETRY_LOST)
        self.assertTrue(self.evaluator.state.telemetry_lost_fired)

        # Second check should return None (already fired)
        self.assertIsNone(self.evaluator.evaluate_timeout(now=1045.0))


class TestNotificationDispatchers(TestCase):
    """Test notification channels: terminal bell, desktop notifications, and webhooks."""

    def test_terminal_bell(self) -> None:
        with patch("sys.stdout.write") as mock_write, patch("sys.stdout.flush") as mock_flush:
            dispatch_terminal_bell()
            mock_write.assert_called_with("\a")
            mock_flush.assert_called()

    @patch("subprocess.Popen")
    def test_desktop_notification_linux(self, mock_popen: MagicMock) -> None:
        with patch("sys.platform", "linux"):
            ok = dispatch_desktop_notification("Train Alert", "IC 52 Departed")
            self.assertTrue(ok)
            mock_popen.assert_called()
            args = mock_popen.call_args[0][0]
            self.assertEqual(args[0], "notify-send")
            self.assertIn("Train Alert", args)

    @patch("subprocess.Popen")
    def test_desktop_notification_darwin(self, mock_popen: MagicMock) -> None:
        with patch("sys.platform", "darwin"):
            ok = dispatch_desktop_notification("Train Alert", "IC 52 Departed")
            self.assertTrue(ok)
            mock_popen.assert_called()
            args = mock_popen.call_args[0][0]
            self.assertEqual(args[0], "osascript")

    @patch("subprocess.Popen")
    def test_desktop_notification_win32(self, mock_popen: MagicMock) -> None:
        with patch("sys.platform", "win32"):
            ok = dispatch_desktop_notification("Train Alert", "IC 52 Departed")
            self.assertTrue(ok)
            mock_popen.assert_called()
            args = mock_popen.call_args[0][0]
            self.assertEqual(args[0], "powershell")

    @patch("urllib.request.urlopen")
    def test_webhook_telegram(self, mock_urlopen: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        config = WatchConfig(
            train_number="IC 52",
            webhook_url="https://api.telegram.org/bot12345/sendMessage",
            webhook_chat_id="999888",
        )
        event = WatchAlertEvent(
            timestamp="2026-11-22 14:00:00",
            train_number="IC 52",
            trigger_type=WatchTriggerType.DEPARTURE,
            headline="DEPARTURE: IC 52",
            message="Train departed Athens",
            current_speed_kmh=45.0,
            current_delay_min=2.0,
            delay_delta_min=0.0,
        )

        ok = dispatch_webhook(config, event)
        self.assertTrue(ok)
        mock_urlopen.assert_called()
        req = mock_urlopen.call_args[0][0]
        data = json.loads(req.data.decode("utf-8"))
        self.assertEqual(data["chat_id"], "999888")
        self.assertIn("IC 52", data["text"])

    @patch("urllib.request.urlopen")
    def test_webhook_discord(self, mock_urlopen: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status = 204
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        config = WatchConfig(
            train_number="IC 52",
            webhook_url="https://discord.com/api/webhooks/123/abc",
        )
        event = WatchAlertEvent(
            timestamp="2026-11-22 14:00:00",
            train_number="IC 52",
            trigger_type=WatchTriggerType.DELAY_INCREASE,
            headline="DELAY: +5m",
            message="Delay increased",
            current_speed_kmh=80.0,
            current_delay_min=5.0,
            delay_delta_min=5.0,
        )

        ok = dispatch_webhook(config, event)
        self.assertTrue(ok)
        req = mock_urlopen.call_args[0][0]
        data = json.loads(req.data.decode("utf-8"))
        self.assertIn("embeds", data)

    @patch("watchdog.dispatch_terminal_bell")
    @patch("watchdog.dispatch_desktop_notification")
    @patch("watchdog.dispatch_webhook")
    def test_dispatch_all_channels(
        self,
        mock_webhook: MagicMock,
        mock_desktop: MagicMock,
        mock_bell: MagicMock,
    ) -> None:
        mock_desktop.return_value = True
        mock_webhook.return_value = True

        config = WatchConfig(
            train_number="IC 52",
            enable_bell=True,
            enable_desktop=True,
            webhook_url="https://example.com/webhook",
        )
        event = WatchAlertEvent(
            timestamp="2026-11-22 14:00:00",
            train_number="IC 52",
            trigger_type=WatchTriggerType.DEPARTURE,
            headline="DEPARTURE",
            message="Departed",
            current_speed_kmh=50.0,
            current_delay_min=0.0,
            delay_delta_min=0.0,
        )

        results = dispatch_all_channels(config, event)
        self.assertTrue(results[NotificationChannel.BELL])
        self.assertTrue(results[NotificationChannel.DESKTOP])
        self.assertTrue(results[NotificationChannel.WEBHOOK])
        mock_bell.assert_called_once()
        mock_desktop.assert_called_once()
        mock_webhook.assert_called_once()


class TestWatchdogFormatters(TestCase):
    """Test console lines and ticker panel formatting."""

    def test_format_watch_event_line(self) -> None:
        event = WatchAlertEvent(
            timestamp="2026-11-22 14:00:00",
            train_number="IC 52",
            trigger_type=WatchTriggerType.DEPARTURE,
            headline="DEPARTURE: Train IC 52 Departed",
            message="Train departed Athens terminal.",
            current_speed_kmh=45.0,
            current_delay_min=0.0,
            delay_delta_min=0.0,
        )
        line = format_watch_event_line(event, ascii_mode=True)
        self.assertIn("2026-11-22 14:00:00", line)
        self.assertIn("DEPARTURE: Train IC 52 Departed", line)

    def test_format_watch_ticker(self) -> None:
        config = WatchConfig(train_number="IC 52", destination_station="Thessaloniki")
        state = WatchState(train_number="IC 52", has_departed=True)
        pos = DummyPosition(speed=112.5, delay=4.0)

        ticker = format_watch_ticker(config, state, pos, ascii_mode=True)
        self.assertIn("RAILMON WATCHDOG", ticker)
        self.assertIn("IC 52", ticker)
        self.assertIn("112.5 km/h", ticker)
        self.assertIn("+4m", ticker)
