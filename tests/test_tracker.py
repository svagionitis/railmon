"""Unit tests for the Hellenic Railways train tracker client, models, and endpoints."""

import io
import json
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


if __name__ == "__main__":
    unittest.main()
