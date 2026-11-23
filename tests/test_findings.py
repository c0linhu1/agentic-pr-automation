import json
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import EngineCase
from vader import config
from vader.engine import Review
from vader.findings import FIELDS, SCHEMA, SEVERITIES, VERIFY_SCHEMA, code_lines, ground, parse_findings, schema_for
from vader.skill_loader import categories

ONE = {"severity": "high", "category": "config", "description": "hardcoded URL"}


class ParseTests(unittest.TestCase):
    def test_schema_matches_what_the_parser_accepts(self):
        item = SCHEMA["properties"]["findings"]["items"]
        self.assertEqual(item["properties"]["severity"]["enum"], SEVERITIES)
        self.assertIn("description", item["required"])

    def test_categories_come_from_the_skills_scope_table(self):
        body = ("## Scope\n\n| Category | Look for |\n|---|---|\n| authz | Missing checks |\n"
                "| secrets | Keys in code |\n\n### Out of Scope\n\n- style\n\n## Severity\n\n| high | Bad |\n")
        self.assertEqual(categories(body), ("authz", "secrets"))
        self.assertEqual(categories("## Process\n\n1. Look.\n"), ())  # no Scope table
        limited = schema_for(("authz", "secrets"))["properties"]["findings"]["items"]["properties"]["category"]
        self.assertEqual(limited["enum"], ["authz", "secrets"])
        self.assertIs(schema_for(()), SCHEMA)  # without a table, any category
        self.assertNotIn("enum", SCHEMA["properties"]["findings"]["items"]["properties"]["category"])

    def test_the_model_explains_before_it_decides(self):
        # The model writes fields in schema order: quote and description before severity, reason before verdict.
        order = list(SCHEMA["properties"]["findings"]["items"]["properties"])
        self.assertEqual(order, FIELDS)
        self.assertLess(order.index("evidence"), order.index("severity"))
        self.assertLess(order.index("description"), order.index("severity"))
        self.assertEqual(list(VERIFY_SCHEMA["properties"]), ["reason", "verdict"])

    def test_only_the_schema_shape_parses(self):
        for raw, count in [(json.dumps({"findings": [ONE]}), 1), ('{"findings": []}', 0)]:
            p = parse_findings(raw)
            self.assertEqual((p.failure, len(p.findings)), (None, count), raw)  # empty is a clean answer
        for raw in ["{}", '{"issues": []}', json.dumps([ONE]), "not json", '"text"']:
            self.assertIsNotNone(parse_findings(raw).failure, raw)


DELIVERY = """diff --git a/src/App.Shipping/Delivery.cs b/src/App.Shipping/Delivery.cs
--- a/src/App.Shipping/Delivery.cs
+++ b/src/App.Shipping/Delivery.cs
@@ -10,4 +10,5 @@ public static class Delivery
     public static DateOnly Deliver(DateOnly order)
     {
-        return order.AddDays(2);
+        var days = order.DayOfWeek == DayOfWeek.Friday ? 4 : 2;
+        return order.AddDays(days);
     }
"""
CONTEXT = "\n".join(f"// line {i}" for i in range(1, 10)) + """
    public static DateOnly Deliver(DateOnly order)
    {
        var days = order.DayOfWeek == DayOfWeek.Friday ? 4 : 2;
        return order.AddDays(days);
    }
    static bool IsHoliday(DateOnly d) => Holidays.Contains(d);"""


class GroundTests(unittest.TestCase):
    """A finding's quote must be code the validator was shown; its file and line come from where the quote is."""

    CODE = {"src/App.Shipping/Delivery.cs": code_lines(DELIVERY, CONTEXT),
            "src/App.Web/Scripts/cart.ts": code_lines("diff --git a/x b/x\n@@ -1,1 +1,2 @@\n const a = 1;\n+let total = 0;\n")}

    def check(self, **finding):
        f = {"severity": "high", "description": "d", "file": None, "line": None, **finding}
        return ground(f, self.CODE), f

    def test_a_quote_from_the_diff_sets_the_file_and_line(self):
        why, f = self.check(file="Delivery.cs", line=3, evidence="var days = order.DayOfWeek == DayOfWeek.Friday ? 4 : 2;")
        self.assertIsNone(why)
        self.assertEqual((f["file"], f["line"]), ("src/App.Shipping/Delivery.cs", 12))  # the model said 3

    def test_quotes_are_matched_loosely_but_must_be_real(self):
        for evidence in ["+        var days = order.DayOfWeek == DayOfWeek.Friday ? 4 : 2;",   # pasted diff marker
                         "var   days =  order.DayOfWeek",                                       # spacing
                         "var days = order.DayOfWeek == DayOfWeek.Friday ? 4 : 2;\n...\nreturn order.AddDays(days);",
                         "{ var days = order.DayOfWeek == DayOfWeek.Friday ? 4 : 2; return order.AddDays(days);",  # rewrapped
                         "-        return order.AddDays(2);",                                    # a removed line
                         "static bool IsHoliday(DateOnly d)"]:                                  # from CONTEXT
            with self.subTest(evidence):
                self.assertIsNone(self.check(file="src/App.Shipping/Delivery.cs", evidence=evidence)[0])
        why, _ = self.check(file="src/App.Shipping/Delivery.cs", evidence="return order.AddBusinessDays(2);")
        self.assertEqual(why, "the code it quotes isn't in src/App.Shipping/Delivery.cs")

    def test_code_squashed_onto_one_line_or_shortened_still_counts(self):
        # Models often quote a multi-line call on one line, with "..." for the rest.
        code = {"src/signup.py": code_lines('@@ -30,2 +30,6 @@\n+    analytics.track("signup", {\n'
                                            '+        "email": user.email,\n+        "full_name": user.full_name,\n'
                                            '+        "date_of_birth": user.date_of_birth,\n+    })\n')}
        for evidence in ['analytics.track("signup", {"email": user.email, "full_name": user.full_name, '
                         '"date_of_birth": user.date_of_birth})',
                         'analytics.track("signup", { "email": user.email, ... })']:
            with self.subTest(evidence):
                f = {"severity": "critical", "description": "d", "file": "signup.py", "line": None, "evidence": evidence}
                self.assertIsNone(ground(f, code))
                self.assertEqual((f["file"], f["line"]), ("src/signup.py", 30))
        for made_up in ['analytics.track("signup", {"email": user.email, "phone": user.phone})',  # a word that isn't there
                        'track(user.email)']:  # the words, but not in that order
            with self.subTest(made_up):
                self.assertIsNotNone(ground({"file": "src/signup.py", "evidence": made_up}, code))

    def test_a_quote_can_join_lines_from_two_files(self):
        # A correct finding can quote a setting and the code that reads it together.
        code = {"src/appsettings.json": code_lines('@@ -4,1 +4,2 @@\n   "Currency": "USD",\n+  "CardFeePercent": 2.9\n'),
                "src/FeeCalculator.cs": code_lines('@@ -0,0 +1,2 @@\n+public class FeeCalculator\n'
                                                   '+    Math.Round(orderTotal * _options.CardFeePercent, 2);\n')}
        f = {"file": "src/FeeCalculator.cs", "line": None,
             "evidence": '"CardFeePercent": 2.9\n    Math.Round(orderTotal * _options.CardFeePercent, 2);'}
        self.assertIsNone(ground(f, code))
        self.assertEqual((f["file"], f["line"]), ("src/appsettings.json", 5))
        made_up = '"CardFeePercent": 2.9\n    Math.Round(orderTotal * _options.CardFeePercent / 100, 2);'
        self.assertIsNotNone(ground({"file": "src/FeeCalculator.cs", "evidence": made_up}, code))

    def test_a_claim_about_the_pr_can_quote_its_description(self):
        # "The description says tests were added, but none were" quotes the description, not code.
        pr_text = "Fix crash when customer has no address\n\nAdds a None check and adds unit tests for it."
        self.assertIsNone(ground({"file": None, "evidence": "adds unit tests for it"}, self.CODE, pr_text))
        self.assertIsNotNone(ground({"file": None, "evidence": "adds integration tests"}, self.CODE, pr_text))

    def test_the_wrong_file_is_corrected_and_a_made_up_one_is_dropped(self):
        why, f = self.check(file="src/Other.cs", evidence="let total = 0;")
        self.assertEqual((why, f["file"], f["line"]), (None, "src/App.Web/Scripts/cart.ts", 2))
        why, _ = self.check(file="src/Billing/Invoice.cs", evidence="invoice.Total -= discount;")
        self.assertEqual(why, "the code it quotes isn't in the PR (it names src/Billing/Invoice.cs)")

    def test_a_wrong_weekday_is_dropped_in_any_date_format(self):
        # Models often get weekdays wrong: 2028-04-13 is a Thursday.
        for wrong in ["Ships on Apr 13, 2028, which is a Wednesday, so the delivery date is off.",
                      "2028-04-13 is a Wednesday", "new DateOnly(2028, 4, 13) (a Wednesday) is skipped",
                      "Wednesday, April 13, 2028 is treated as a holiday", "13 April 2028 falls on a Wednesday"]:
            with self.subTest(wrong):
                self.assertEqual(self.check(description=wrong)[0],
                                 "it says 2028-04-13 is a Wednesday, but it's a Thursday")
        for fine in ["2028-04-13 is a Thursday, and the code adds 2 days", "2028-04-13 is not a Wednesday",
                     "treats 2028-04-14 as a Monday", "2028-02-30 is a Monday", "fails on Fridays after 2028-04-13"]:
            with self.subTest(fine):
                self.assertIsNone(self.check(description=fine)[0])

    def test_a_finding_without_a_quote_is_kept_as_it_is(self):
        # Something missing (a test, a check) has no code to quote.
        for evidence in [None, "", "}", "..."]:
            with self.subTest(evidence=evidence):
                why, f = self.check(file="tests/DeliveryTests.cs", line=7, evidence=evidence)
                self.assertEqual((why, f["file"], f["line"]), (None, "tests/DeliveryTests.cs", 7))


class RetryTests(EngineCase):
    def run_review(self, replies):
        calls = []

        def complete(*a, **k):
            calls.append(1)
            return replies[min(len(calls), len(replies)) - 1]

        with self.assertLogs("vader.engine", "WARNING"):
            review = self.finish(Review(source="slack", validators=["architecture"], diff="+x"), complete=complete)
        return review, len(calls)

    def test_a_bad_reply_is_asked_again_and_saved(self):
        saved = Path(self.tmp.name) / "failed"
        with mock.patch.object(config, "FAILED_REPLIES_DIR", saved):
            review, calls = self.run_review(["{}", json.dumps({"findings": [ONE]})])
        self.assertEqual((calls, review.results[0]["status"]), (2, "ok"))
        files = list(saved.iterdir())
        self.assertEqual(len(files), 1)
        self.assertIn("JSON had no findings list", files[0].read_text(encoding="utf-8"))

    def test_still_fails_if_the_retry_is_bad_too(self):
        review, calls = self.run_review(["{}", "{}"])
        self.assertEqual((calls, review.results[0]["status"], review.verdict), (2, "failed", "HOLD"))


if __name__ == "__main__":
    unittest.main()
