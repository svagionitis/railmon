"""Unit tests for the Hellenic Railways train tracker client and models."""

import unittest
from train_tracker import TrainPosition


class TestTrainPositionModel(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
