import unittest

from scripts.evaluate_detector import summarize


class EvaluationTests(unittest.TestCase):
    def test_metrics_and_hard_negative_groups(self):
        rows = [
            {"category": "bark", "is_bark": True, "score": 0.9},
            {"category": "bark", "is_bark": True, "score": 0.2},
            {"category": "howl", "is_bark": False, "score": 0.8},
            {"category": "speech", "is_bark": False, "score": 0.1},
        ]
        report = summarize(rows, 0.5)
        self.assertEqual(report["overall"]["false_positives"], 1)
        self.assertEqual(report["overall"]["false_negatives"], 1)
        self.assertEqual(report["overall"]["f1"], 0.5)
        self.assertEqual(report["by_negative_category"]["howl"]["false_positives"], 1)
        self.assertEqual(report["by_negative_category"]["speech"]["false_positives"], 0)
