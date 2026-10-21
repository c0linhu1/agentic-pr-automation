import unittest

from vader.decision import AUTO_FIX, BLOCK, HOLD, PASS, decide


def ok(*severities):
    return {"status": "ok", "findings": [{"severity": s} for s in severities]}


FAILED = {"status": "failed", "findings": []}


class DecideTests(unittest.TestCase):
    def test_critical_blocks(self):
        self.assertEqual(decide([ok("critical"), ok("high"), FAILED])[0], BLOCK)

    def test_high_holds(self):
        self.assertEqual(decide([ok("high", "low"), ok()])[0], HOLD)

    def test_failed_validator_holds_even_with_no_findings(self):
        verdict, reason = decide([ok(), FAILED])
        self.assertEqual(verdict, HOLD)
        self.assertIn("missing", reason)

    def test_missing_ci_holds_only_when_required(self):
        self.assertEqual(decide([ok()], ci_status=None, require_ci=True)[0], HOLD)
        self.assertEqual(decide([ok()], ci_status=None, require_ci=False)[0], PASS)

    def test_ci_failed_auto_fixes(self):
        self.assertEqual(decide([ok()], ci_status="failed")[0], AUTO_FIX)

    def test_medium_or_low_auto_fixes(self):
        self.assertEqual(decide([ok("medium")])[0], AUTO_FIX)
        self.assertEqual(decide([ok("low")])[0], AUTO_FIX)

    def test_info_only_passes(self):
        self.assertEqual(decide([ok("info"), ok()], ci_status="passed")[0], PASS)

    def test_no_validators_holds(self):
        self.assertEqual(decide([])[0], HOLD)

    def test_partial_validator_still_blocks_on_a_critical_it_found(self):
        partial = {"status": "partial", "findings": [{"severity": "critical"}]}
        self.assertEqual(decide([ok(), partial])[0], BLOCK)

    def test_partial_validator_with_only_low_findings_holds(self):
        partial = {"status": "partial", "findings": [{"severity": "low"}]}
        verdict, reason = decide([ok(), partial])
        self.assertEqual(verdict, HOLD)
        self.assertIn("didn't finish", reason)


if __name__ == "__main__":
    unittest.main()
