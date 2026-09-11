"""State-transition checks without touching production or sending requests."""
import unittest

from green_public_monitor import advance, classify


class MonitorTests(unittest.TestCase):
    def healthy_probe(self):
        return {"name": "health", "status": 200, "error": "", "seconds": 0.1, "slow_seconds": 3}

    def test_error_payload_is_down_even_when_http_is_200(self):
        probe = self.healthy_probe()
        probe["error"] = "wrong_health_payload"
        self.assertEqual(classify([probe], [])[0], "down")

    def test_real_radar_latency_and_backup_are_reported(self):
        state, reasons = classify([self.healthy_probe()], [{"path": "/api/competitors/own-store", "status": 200, "seconds": 12, "upstream": "100.70.103.11:8501"}])
        self.assertEqual(state, "degraded")
        self.assertIn("using_tailscale_backup", reasons)
        self.assertTrue(any("12s" in reason for reason in reasons))

    def test_one_spike_does_not_alert_and_recovery_needs_two_samples(self):
        first = advance({}, "down", ["health:timeout"], 1)
        self.assertEqual(first["sequence"], 0)
        second = advance(first, "down", ["health:timeout"], 2)
        self.assertEqual((second["state"], second["sequence"]), ("down", 1))
        third = advance(second, "down", ["health:timeout"], 3)
        self.assertEqual(third["sequence"], 1)
        fourth = advance(third, "healthy", [], 4)
        self.assertEqual(fourth["state"], "down")
        fifth = advance(fourth, "healthy", [], 5)
        self.assertEqual((fifth["state"], fifth["sequence"]), ("healthy", 2))

    def test_expected_anonymous_401_is_not_a_business_alarm(self):
        self.assertEqual(classify([self.healthy_probe()], [{"path": "/api/competitors", "status": 401, "seconds": 0.1, "upstream": "127.0.0.1:18503"}]), ("healthy", []))


if __name__ == "__main__":
    unittest.main()
