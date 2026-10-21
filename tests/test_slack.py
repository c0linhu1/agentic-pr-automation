import unittest

from slack_bot import fmt


class SlackTextTests(unittest.TestCase):
    """Slack sends &, <, and > in message text as &amp;, &lt;, and &gt;, and reads them the same way."""

    def test_pasted_code_gets_its_angle_brackets_and_ampersands_back(self):
        sent = "+    var xs = new List&lt;int&gt;();\n+    if (a &amp;&amp; b) return;\n+    x = \"&amp;lt;\";"
        self.assertEqual(fmt.unescape(sent),
                         "+    var xs = new List<int>();\n+    if (a && b) return;\n+    x = \"&lt;\";")

    def test_pr_title_and_author_cant_ping_the_channel(self):
        review = {"title": "<!channel> urgent", "meta": {"url": "https://github.com/o/r/pull/1", "repo": "o/r",
                                                         "number": 1, "author": "<@U123>"}}
        text = fmt.pr_header(review, 1)
        self.assertNotIn("<!channel>", text)
        self.assertNotIn("<@U123>", text)
        self.assertIn("&lt;!channel&gt; urgent", text)
        self.assertIn("<https://github.com/o/r/pull/1|o/r#1>", text)  # Vader's own link still works

    def test_findings_cant_ping_or_hide_links(self):
        finding = {"severity": "high", "category": "authz", "file": "a.cs", "line": 1,
                   "description": "<!here> see <https://evil.example|docs>", "suggestedFix": "x & y"}
        result = {"validator": "security", "display_name": "Security Validator", "icon": ":shield:",
                  "status": "ok", "findings": [finding], "error": None, "seconds": 1.0, "model": "m:1b",
                  "rejected": [{"severity": "high", "description": "<!channel>", "reason": "<!here>"}]}
        review = {"verdict": "HOLD", "verdict_reason": "1 high finding(s)", "started": 0, "finished": 1,
                  "validators": ["security"], "model": "m:1b", "models": {}, "meta": {}, "results": [result]}
        for text in (fmt.validator_result(result), fmt.verdict(review)):
            self.assertNotIn("<!here>", text)
            self.assertNotIn("<!channel>", text)
            self.assertNotIn("<https://evil", text)
        self.assertIn("x &amp; y", fmt.validator_result(result))


if __name__ == "__main__":
    unittest.main()
