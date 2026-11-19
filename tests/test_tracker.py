"""Unit tests for the Hellenic Railways train tracker client, models, and endpoints."""

import io
import json
import threading
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from train_tracker import RailwayGovClient, TrainPosition


class TestTrainPositionModel(unittest.TestCase):
    """Test suite for TrainPosition deserialization and defaults."""

    def test_from_dict_complete_payload(self):
        payload = {
            "id": "6afc0821f55acaffdcf89c86",
            "trainNumber": "2232",
            "name": "2232",
            "locomotiveNumber": "460103",
            "trainType": "passenger",
            "serviceType": "Suburban",
            "origin": "Άνω Λιόσια",
            "destination": "Αεροδρόμιο",
            "nextStation": "Νερατζιώτισσα",
            "lat": 38.050842,
            "lng": 23.782449,
            "speed": 116.99,
            "course": 128.78,
            "trackHeading": 128.78,
            "altitude": 230.85,
            "delay": 2,
            "progress": 0.2598,
            "chainage_m": 29300,
            "corridor": "PIRAIR",
            "distanceToDestination_m": 26135,
            "timestamp": "2026-11-18T11:46:59.004Z",
            "scheduleId": "6a94744ed16c475ca144c4b7",
        }

        train = TrainPosition.from_dict(payload)
        self.assertEqual(train.id, "6afc0821f55acaffdcf89c86")
        self.assertEqual(train.train_number, "2232")
        self.assertEqual(train.status, "Κανονική")
        self.assertAlmostEqual(train.lat, 38.050842)
        self.assertAlmostEqual(train.lng, 23.782449)
        self.assertEqual(train.delay, 2)
        self.assertEqual(train.corridor, "PIRAIR")

    def test_from_dict_stationary_train(self):
        payload = {
            "id": "yard_train",
            "trainNumber": "120010",
            "lat": 40.6448,
            "lng": 22.9296,
            "speed": 0.0,
        }

        train = TrainPosition.from_dict(payload)
        self.assertEqual(train.status, "Στάση")
        self.assertEqual(train.delay, 0)
        self.assertIsNone(train.origin)

    def test_from_dict_valid_coordinates_boundary_limits(self):
        """TC-GEO-01: Boundary limit coordinates (-90, -180) and (90, 180) pass validation."""
        p_min = {"id": "min", "trainNumber": "1", "lat": -90.0, "lng": -180.0}
        t_min = TrainPosition.from_dict(p_min)
        self.assertEqual(t_min.lat, -90.0)
        self.assertEqual(t_min.lng, -180.0)

        p_max = {"id": "max", "trainNumber": "2", "lat": 90.0, "lng": 180.0}
        t_max = TrainPosition.from_dict(p_max)
        self.assertEqual(t_max.lat, 90.0)
        self.assertEqual(t_max.lng, 180.0)

    def test_from_dict_zero_zero_coordinates_preserved(self):
        """TC-GEO-02: Coordinates (0.0, 0.0) are valid and preserved without truthiness drops."""
        payload = {"id": "null_island", "trainNumber": "0000", "lat": 0.0, "lng": 0.0}
        train = TrainPosition.from_dict(payload)
        self.assertEqual(train.lat, 0.0)
        self.assertEqual(train.lng, 0.0)

    def test_from_dict_rejects_missing_or_none_coordinates(self):
        """TC-GEO-03: Missing or None lat/lng raises ValueError."""
        with self.assertRaises(ValueError):
            TrainPosition.from_dict({"id": "t1", "lat": None, "lng": 23.5})
        with self.assertRaises(ValueError):
            TrainPosition.from_dict({"id": "t2", "lat": 38.5, "lng": None})
        with self.assertRaises(ValueError):
            TrainPosition.from_dict({"id": "t3", "lat": 38.5})  # missing lng
        with self.assertRaises(ValueError):
            TrainPosition.from_dict({"id": "t4", "lng": 23.5})  # missing lat

    def test_from_dict_rejects_out_of_bounds_latitude(self):
        """TC-GEO-04: Latitudes outside [-90.0, 90.0] raise ValueError."""
        with self.assertRaises(ValueError):
            TrainPosition.from_dict({"id": "t", "lat": 90.0001, "lng": 23.5})
        with self.assertRaises(ValueError):
            TrainPosition.from_dict({"id": "t", "lat": -90.0001, "lng": 23.5})
        with self.assertRaises(ValueError):
            TrainPosition.from_dict({"id": "t", "lat": 999.0, "lng": 23.5})

    def test_from_dict_rejects_out_of_bounds_longitude(self):
        """TC-GEO-05: Longitudes outside [-180.0, 180.0] raise ValueError."""
        with self.assertRaises(ValueError):
            TrainPosition.from_dict({"id": "t", "lat": 38.5, "lng": 180.0001})
        with self.assertRaises(ValueError):
            TrainPosition.from_dict({"id": "t", "lat": 38.5, "lng": -180.0001})
        with self.assertRaises(ValueError):
            TrainPosition.from_dict({"id": "t", "lat": 38.5, "lng": -999.0})

    def test_from_dict_rejects_nan_and_inf_coordinates(self):
        """TC-GEO-06: NaN and Infinite coordinates raise ValueError."""
        with self.assertRaises(ValueError):
            TrainPosition.from_dict({"id": "t", "lat": float("nan"), "lng": 23.5})
        with self.assertRaises(ValueError):
            TrainPosition.from_dict({"id": "t", "lat": 38.5, "lng": float("nan")})
        with self.assertRaises(ValueError):
            TrainPosition.from_dict({"id": "t", "lat": float("inf"), "lng": 23.5})
        with self.assertRaises(ValueError):
            TrainPosition.from_dict({"id": "t", "lat": 38.5, "lng": -float("inf")})

    def test_from_dict_defensive_numeric_speed(self):
        """TC-PARSE-01: Malformed speed values coerce safely to non-negative floats."""
        # Non-numeric string defaults to 0.0
        t1 = TrainPosition.from_dict({"id": "t", "lat": 38.0, "lng": 23.0, "speed": "fast"})
        self.assertEqual(t1.speed, 0.0)
        self.assertEqual(t1.status, "Στάση")

        # None defaults to 0.0
        t2 = TrainPosition.from_dict({"id": "t", "lat": 38.0, "lng": 23.0, "speed": None})
        self.assertEqual(t2.speed, 0.0)

        # Negative speed clamps to 0.0
        t3 = TrainPosition.from_dict({"id": "t", "lat": 38.0, "lng": 23.0, "speed": -50.0})
        self.assertEqual(t3.speed, 0.0)

        # String numeric parses correctly
        t4 = TrainPosition.from_dict({"id": "t", "lat": 38.0, "lng": 23.0, "speed": "120.5"})
        self.assertEqual(t4.speed, 120.5)
        self.assertEqual(t4.status, "Κανονική")

    def test_from_dict_defensive_numeric_delay(self):
        """TC-PARSE-02: Malformed delay values coerce safely to integer without raising."""
        # String float "5.0"
        t1 = TrainPosition.from_dict({"id": "t", "lat": 38.0, "lng": 23.0, "delay": "5.0"})
        self.assertEqual(t1.delay, 5)

        # Non-numeric string "delayed" defaults to 0
        t2 = TrainPosition.from_dict({"id": "t", "lat": 38.0, "lng": 23.0, "delay": "delayed"})
        self.assertEqual(t2.delay, 0)

        # None defaults to 0
        t3 = TrainPosition.from_dict({"id": "t", "lat": 38.0, "lng": 23.0, "delay": None})
        self.assertEqual(t3.delay, 0)

    def test_from_dict_defensive_optional_fields(self):
        """TC-PARSE-03: Corrupted optional fields parse safely to None without crashing."""
        payload = {
            "id": "t",
            "lat": 38.0,
            "lng": 23.0,
            "altitude": "high",
            "progress": "halfway",
            "chainage_m": "twenty_km",
            "distanceToDestination_m": "far",
            "corridor": "   ",
        }
        train = TrainPosition.from_dict(payload)
        self.assertIsNone(train.altitude)
        self.assertIsNone(train.progress)
        self.assertIsNone(train.chainage_m)
        self.assertIsNone(train.distance_to_destination_m)
        self.assertIsNone(train.corridor)

    def test_from_dict_rejects_non_dict_payload(self):
        """TC-PARSE-04: Non-dict payloads raise TypeError."""
        with self.assertRaises(TypeError):
            TrainPosition.from_dict(None)  # type: ignore
        with self.assertRaises(TypeError):
            TrainPosition.from_dict("invalid_payload")  # type: ignore
        with self.assertRaises(TypeError):
            TrainPosition.from_dict([1, 2, 3])  # type: ignore


class TestRailwayGovClient(unittest.TestCase):
    """Test suite for RailwayGovClient REST and SSE streaming methods."""

    def setUp(self):
        self.client = RailwayGovClient("https://railway.gov.gr")

    def _mock_http_response(self, json_data: dict):
        """Helper creating a mock urllib response returning json_data."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(json_data).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        return mock_resp

    @patch("urllib.request.urlopen")
    def test_get_upcoming_schedules(self, mock_urlopen):
        """TC-NET-01: Fetch upcoming schedules returns schedule list."""
        mock_urlopen.return_value = self._mock_http_response({
            "schedules": [
                {"trainNumber": "1597", "origin": "Athens", "destination": "Thessaloniki"}
            ]
        })
        schedules = self.client.get_upcoming_schedules()
        self.assertEqual(len(schedules), 1)
        self.assertEqual(schedules[0]["trainNumber"], "1597")

    @patch("urllib.request.urlopen")
    def test_get_schedule_by_id(self, mock_urlopen):
        """TC-NET-02: Fetch itinerary by schedule ID."""
        mock_urlopen.return_value = self._mock_http_response({
            "id": "sch_123",
            "trainNumber": "50",
            "stops": ["SKA", "Thiva", "Larissa"],
        })
        schedule = self.client.get_schedule_by_id("sch_123")
        self.assertEqual(schedule["id"], "sch_123")
        self.assertEqual(schedule["trainNumber"], "50")

    @patch("urllib.request.urlopen")
    def test_get_schedule_by_train_number(self, mock_urlopen):
        """TC-NET-03: Fetch schedule by commercial train number."""
        mock_urlopen.return_value = self._mock_http_response({
            "trainNumber": "1597",
            "service": "InterCity",
        })
        res = self.client.get_schedule_by_train_number("1597")
        self.assertEqual(res["trainNumber"], "1597")

    @patch("urllib.request.urlopen")
    def test_get_station_and_boards(self, mock_urlopen):
        """TC-NET-04: Fetch station details, departure board, and arrival board."""
        # Station details
        mock_urlopen.return_value = self._mock_http_response({
            "id": "st_ath",
            "name": "Athens",
        })
        st = self.client.get_station("st_ath")
        self.assertEqual(st["name"], "Athens")

        # Departures
        mock_urlopen.return_value = self._mock_http_response({
            "departures": [{"trainNumber": "52", "departureTime": "14:30"}]
        })
        deps = self.client.get_station_departures("st_ath", limit=5)
        self.assertEqual(len(deps), 1)
        self.assertEqual(deps[0]["trainNumber"], "52")

        # Arrivals
        mock_urlopen.return_value = self._mock_http_response({
            "arrivals": [{"trainNumber": "53", "arrivalTime": "15:00"}]
        })
        arrs = self.client.get_station_arrivals("st_ath", limit=5)
        self.assertEqual(len(arrs), 1)
        self.assertEqual(arrs[0]["trainNumber"], "53")

    @patch("urllib.request.urlopen")
    def test_get_rail_infrastructure(self, mock_urlopen):
        """TC-NET-05: Fetch rail infrastructure GeoJSON layers."""
        mock_urlopen.return_value = self._mock_http_response({
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": {"name": "Track A"}}],
        })
        infra = self.client.get_rail_infrastructure(types="track", limit=10)
        self.assertEqual(infra["type"], "FeatureCollection")
        self.assertEqual(len(infra["features"]), 1)

    @patch("urllib.request.urlopen")
    def test_stream_train_positions_sse(self, mock_urlopen):
        """TC-NET-06: Parse Server-Sent Events stream and filter trainPositionsUx."""
        sse_lines = [
            b"event: ping\n",
            b"data: {}\n",
            b"\n",
            b"event: trainPositionsUx\n",
            b'data: {"positions": [{"id": "t1", "trainNumber": "1597", "lat": 38.5, "lng": 23.5, "speed": 120.0}]}\n',
            b"\n",
        ]

        mock_resp = MagicMock()
        mock_resp.__iter__.return_value = iter(sse_lines)
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        stream = self.client.stream_train_positions()
        batch = next(stream)

        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0].id, "t1")
        self.assertEqual(batch[0].train_number, "1597")
        self.assertAlmostEqual(batch[0].lat, 38.5)
        self.assertAlmostEqual(batch[0].lng, 23.5)
        self.assertEqual(batch[0].status, "Κανονική")

    @patch("urllib.request.urlopen")
    def test_stream_train_positions_raises_when_reconnect_disabled(self, mock_urlopen):
        """TC-NET-07: stream_train_positions raises URLError directly without printing to stdout when reconnect_delay <= 0."""
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            stream = self.client.stream_train_positions(reconnect_delay=0.0)
            with self.assertRaises(urllib.error.URLError):
                next(stream)
            self.assertEqual(mock_stdout.getvalue(), "")

    @patch("urllib.request.urlopen")
    def test_stream_train_positions_logs_warning_and_honors_stop_event_on_retry(self, mock_urlopen):
        """TC-NET-08: stream_train_positions logs warning (no stdout print) and exits cleanly via stop_event during retry wait."""
        stop_event = threading.Event()

        def fail_and_schedule_stop(*args, **kwargs):
            # Trigger stop shortly after entering retry wait
            threading.Timer(0.02, stop_event.set).start()
            raise TimeoutError("Read timed out")

        mock_urlopen.side_effect = fail_and_schedule_stop

        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            with self.assertLogs("railmon.train_tracker", level="WARNING") as log_ctx:
                stream = self.client.stream_train_positions(
                    reconnect_delay=5.0,
                    stop_event=stop_event,
                )
                batches = list(stream)

            self.assertEqual(batches, [])
            self.assertEqual(mock_stdout.getvalue(), "")
            self.assertTrue(any("Read timed out" in msg for msg in log_ctx.output))

    @patch("urllib.request.urlopen")
    def test_stream_train_positions_aborts_mid_stream_on_stop_event(self, mock_urlopen):
        """TC-NET-09: stream_train_positions checks stop_event between SSE lines and terminates immediately."""
        stop_event = threading.Event()
        sse_lines = [
            b"event: trainPositionsUx\n",
            b'data: {"positions": [{"id": "t1", "trainNumber": "1597", "lat": 38.5, "lng": 23.5, "speed": 120.0}]}\n',
            b"\n",
            b"event: trainPositionsUx\n",
            b'data: {"positions": [{"id": "t2", "trainNumber": "1598", "lat": 39.0, "lng": 22.5, "speed": 100.0}]}\n',
            b"\n",
        ]

        mock_resp = MagicMock()
        mock_resp.__iter__.return_value = iter(sse_lines)
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        stream = self.client.stream_train_positions(
            reconnect_delay=0.0,
            stop_event=stop_event,
        )
        first_batch = next(stream)
        self.assertEqual(len(first_batch), 1)
        self.assertEqual(first_batch[0].id, "t1")

        # Signal stop before advancing to second event
        stop_event.set()
        with self.assertRaises(StopIteration):
            next(stream)

    @patch("urllib.request.urlopen")
    def test_stream_train_positions_skips_malformed_json_and_continues(self, mock_urlopen):
        """TC-NET-10: Corrupted JSON payloads are logged at DEBUG and skipped without crashing."""
        sse_lines = [
            b"event: trainPositionsUx\n",
            b"data: {corrupted_json_payload\n",
            b"\n",
            b"event: trainPositionsUx\n",
            b'data: {"positions": [{"id": "t1", "trainNumber": "IC50", "lat": 38.0, "lng": 23.7, "speed": 90.0}]}\n',
            b"\n",
        ]

        mock_resp = MagicMock()
        mock_resp.__iter__.return_value = iter(sse_lines)
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        with self.assertLogs("railmon.train_tracker", level="DEBUG") as log_ctx:
            stream = self.client.stream_train_positions(reconnect_delay=0.0)
            batch = next(stream)

        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0].train_number, "IC50")
        self.assertTrue(any("Skipping malformed SSE JSON payload" in msg for msg in log_ctx.output))

    def test_client_init_ssrf_allowed_host(self):
        """TC-SSRF-01: Valid official domain endpoints initialize successfully."""
        c1 = RailwayGovClient("https://railway.gov.gr")
        self.assertEqual(c1.base_url, "https://railway.gov.gr")

        c2 = RailwayGovClient("https://railway.gov.gr/")
        self.assertEqual(c2.base_url, "https://railway.gov.gr")

        c3 = RailwayGovClient()
        self.assertEqual(c3.base_url, "https://railway.gov.gr")

    def test_client_init_ssrf_rejects_insecure_or_untrusted_urls(self):
        """TC-SSRF-02: Plaintext HTTP, untrusted hosts, spoofed domains, and file schemes raise ValueError."""
        invalid_urls = [
            "http://railway.gov.gr",                   # Plaintext HTTP
            "https://attacker.org",                    # Untrusted third party
            "https://169.254.169.254",                 # Cloud metadata IP
            "https://railway.gov.gr.evil.com",         # Subdomain prefix spoofing
            "https://evil-railway.gov.gr",             # Domain similarity spoofing
            "file:///etc/passwd",                      # Local file protocol
            "ftp://railway.gov.gr",                    # Insecure legacy protocol
            "https://railway.gov.gr:8443",             # Non-standard port
            "",                                        # Empty string
            "   ",                                     # Blank whitespace
        ]
        for url in invalid_urls:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    RailwayGovClient(url)

    @patch("urllib.request.urlopen")
    def test_stream_train_positions_filters_corrupted_records_and_preserves_valid(self, mock_urlopen):
        """TC-NET-11: Malformed and out-of-bounds position records are skipped while valid records in same batch pass."""
        sse_lines = [
            b"event: trainPositionsUx\n",
            b'data: {"positions": ['
            b'{"id": "t1", "trainNumber": "1597", "lat": 38.5, "lng": 23.5, "speed": 100.0}, '
            b'{"id": "t2", "lat": 999.0, "lng": 23.5}, '
            b'{"id": "t3", "lat": "corrupted", "lng": 23.5}, '
            b'"not_a_dict", '
            b'{"id": "t4", "lat": 38.0} '
            b"]}\n",
            b"\n",
        ]
        mock_resp = MagicMock()
        mock_resp.__iter__.return_value = iter(sse_lines)
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        with self.assertLogs("railmon.train_tracker", level="DEBUG") as log_ctx:
            stream = self.client.stream_train_positions(reconnect_delay=0.0)
            batch = next(stream)

        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0].id, "t1")
        self.assertEqual(batch[0].train_number, "1597")
        self.assertTrue(any("Skipping invalid train position record" in msg for msg in log_ctx.output))
        self.assertTrue(any("Skipping non-dict position item" in msg for msg in log_ctx.output))

    @patch("urllib.request.urlopen")
    def test_stream_train_positions_preserves_zero_zero_record(self, mock_urlopen):
        """TC-NET-12: Position with lat: 0.0, lng: 0.0 is not dropped by stream truthiness filtering."""
        sse_lines = [
            b"event: trainPositionsUx\n",
            b'data: {"positions": [{"id": "t_zero", "trainNumber": "0000", "lat": 0.0, "lng": 0.0, "speed": 0.0}]}\n',
            b"\n",
        ]
        mock_resp = MagicMock()
        mock_resp.__iter__.return_value = iter(sse_lines)
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        stream = self.client.stream_train_positions(reconnect_delay=0.0)
        batch = next(stream)
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0].id, "t_zero")
        self.assertEqual(batch[0].lat, 0.0)
        self.assertEqual(batch[0].lng, 0.0)

    @patch("urllib.request.urlopen")
    def test_stream_train_positions_handles_non_dict_or_non_list_payloads(self, mock_urlopen):
        """TC-NET-13: Non-dict payload or non-list positions field are skipped cleanly."""
        sse_lines = [
            b"event: trainPositionsUx\n",
            b'data: ["unexpected_list_root"]\n',
            b"\n",
            b"event: trainPositionsUx\n",
            b'data: {"positions": "not_a_list"}\n',
            b"\n",
            b"event: trainPositionsUx\n",
            b'data: {"positions": [{"id": "t_valid", "lat": 38.0, "lng": 23.0}]}\n',
            b"\n",
        ]
        mock_resp = MagicMock()
        mock_resp.__iter__.return_value = iter(sse_lines)
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        with self.assertLogs("railmon.train_tracker", level="DEBUG"):
            stream = self.client.stream_train_positions(reconnect_delay=0.0)
            batch = next(stream)

        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0].id, "t_valid")


if __name__ == "__main__":
    unittest.main()
