import unittest

from vader.decision import BLOCK, HOLD, MINOR, PASS, decide


def ok(*severities):
    return {"status": "ok", "findings": [{"severity": s} for s in severities]}


def partial(*severities):
    return {"status": "partial", "findings": [{"severity": s} for s in severities]}


FAILED = {"status": "failed", "findings": []}


class DecideTests(unittest.TestCase):
    def test_every_rule(self):
        # (results, CI status, CI required) -> (verdict, words the reason must contain)
        cases = [
            ([ok("critical"), ok("high"), FAILED], None, False, BLOCK, "1 critical"),
            ([ok(), partial("critical")], None, False, BLOCK, "critical"),  # a partial validator's findings count
            ([ok("high", "low"), ok()], None, False, HOLD, "1 high"),
            ([ok(), FAILED], None, False, HOLD, "evidence is missing"),
            ([ok(), partial("low")], None, False, HOLD, "didn't finish"),  # partial is missing evidence too
            ([ok()], None, True, HOLD, "CI results are missing"),
            ([ok("high")], "failed", False, HOLD, "1 high"),  # a high outranks failed CI
            ([], None, False, HOLD, "no validators ran"),
            ([ok()], "failed", False, MINOR, "CI failed"),
            ([ok("medium")], None, False, MINOR, "1 medium and 0 low"),
            ([ok("low")], None, False, MINOR, "0 medium and 1 low"),
            ([ok("medium")], "failed", False, MINOR, "1 medium and 0 low finding(s); CI failed"),  # both said
            ([ok("info"), ok()], "passed", False, PASS, "CI passed"),
            ([ok()], None, False, PASS, "no CI results"),
        ]
        for results, ci, require, verdict, reason in cases:
            with self.subTest(results=results, ci=ci, require_ci=require):
                got, why = decide(results, ci_status=ci, require_ci=require)
                self.assertEqual(got, verdict)
                self.assertIn(reason, why)


if __name__ == "__main__":
    unittest.main()
