"""The Slack bot's commands: what each /vader command asks Vader for, and what it says back."""
import importlib
import os
import sys
import unittest
from unittest import mock

from slack_bot.vader_client import VaderError, VaderUnavailable


def load_bot():
    """Import bot.py with fake tokens, without .env.slack or a connection to Slack."""
    env = {"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_APP_TOKEN": "xapp-test", "VADER_API_KEY": "k",
           "SLACK_NOTIFY_CHANNEL": ""}
    with mock.patch.dict(os.environ, env), mock.patch("dotenv.load_dotenv"), mock.patch("slack_bolt.App"):
        sys.modules.pop("slack_bot.bot", None)
        return importlib.import_module("slack_bot.bot")


bot = load_bot()


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.vader = self.enterContext(mock.patch.object(bot, "vader"))
        self.vader.submit_pr.return_value = {"id": "r1", "position": 1, "title": "Add search", "meta": {
            "url": "https://github.com/o/r/pull/42", "repo": "o/r", "number": 42, "author": "dev"}}
        self.vader.submit.return_value = {"id": "r2", "position": 1}
        self.private, self.public = [], []

    def command(self, text):
        bot.handle_command(text, "U1", "C1", None, private=self.private.append, public=self.public.append)

    def test_pr_reviews(self):
        self.command("pr 42")
        self.assertEqual(self.vader.submit_pr.call_args.args[:2], ("42", None))  # all six validators
        self.command("pr 42 security")
        self.assertEqual(self.vader.submit_pr.call_args.args[:2], ("42", ["security"]))
        self.command("pr")
        self.assertIn("Usage: `/vader pr", self.private[-1])

    def test_pasted_reviews(self):
        self.command("run qa +x &amp;&amp; y")  # Slack's escaping is undone before Vader sees the code
        self.assertEqual(self.vader.submit.call_args.args[:2], ("+x && y", ["qa"]))
        self.command("review TITLE: t\nDIFF:\n+x")
        self.assertEqual(self.vader.submit.call_args.args[:2], ("TITLE: t\nDIFF:\n+x", None))
        self.vader.submit.reset_mock()
        self.command("review")
        self.command("run")
        self.vader.submit.assert_not_called()
        self.assertIn("Give me something to review", self.private[0])
        self.assertIn("Usage: `/vader run", self.private[1])

    def test_controls(self):
        self.vader.control.return_value = {"running": None}
        self.command("stop")
        self.vader.control.assert_called_with("stop")
        self.assertIn("stopped Vader", self.public[-1])
        self.vader.automation.return_value = {"automation": True, "github_configured": True}
        self.command("auto on")
        self.vader.automation.assert_called_with(True)
        self.command("auto maybe")
        self.assertIn("Usage: `/vader auto on`", self.private[-1])
        self.command("model gpt-oss:20b")
        self.vader.switch_model.assert_called_with("gpt-oss:20b")

    def test_help_unknown_commands_and_vader_being_down(self):
        self.command("")
        self.assertIn("*Vader* runs six validators", self.private[-1])
        self.command("bogus")
        self.assertIn("Unknown command `bogus`", self.private[-1])
        self.vader.status.side_effect = VaderUnavailable("refused")
        self.command("status")
        self.assertEqual(self.private[-1], bot.NOT_RUNNING)
        self.vader.switch_model.side_effect = VaderError("`x:1b` isn't in the allowed list")
        self.command("model x:1b")
        self.assertEqual(self.private[-1], ":x: `x:1b` isn't in the allowed list")

    def test_a_mention_is_read_as_a_command(self):
        self.assertEqual(bot.LEADING_MENTIONS.sub("", "<@U0VADER> <@U0OTHER> pr 42"), "pr 42")


class EventTests(unittest.TestCase):
    def test_an_automatic_review_gets_its_own_thread(self):
        vader = self.enterContext(mock.patch.object(bot, "vader"))
        post = self.enterContext(mock.patch.object(bot, "post", return_value={"channel": "C1", "ts": "9.9"}))
        review = {"id": "r3", "source": "github", "reply_to": None, "title": "t",
                  "meta": {"url": "https://github.com/o/r/pull/4", "repo": "o/r", "number": 4, "author": "dev"}}
        bot.handle_event({"type": "review_queued", "review": review, "position": 1})
        thread = {"channel": "C1", "thread_ts": "9.9"}
        vader.set_reply_to.assert_called_once_with("r3", thread)  # so the results go to that thread
        bot.handle_event({"type": "review_started", "review": dict(review, validators=["qa"], models={}, model="m",
                                                                    files=[], parts=1, skipped_files=[])})
        self.assertEqual(post.call_args.args[0], thread)
        bot.threads.clear()


if __name__ == "__main__":
    unittest.main()
