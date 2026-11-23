import unittest
from unittest import mock

import httpx
import openai

from vader import config, llm, models

REQUEST = httpx.Request("POST", "http://localhost:11434/v1/chat/completions")  # what the SDK's errors carry


def reply(prompt_tokens=None, completion_tokens=50, finish="stop"):
    usage = None if prompt_tokens is None else mock.Mock(prompt_tokens=prompt_tokens,
                                                         completion_tokens=completion_tokens)
    choice = mock.Mock(finish_reason=finish, message=mock.Mock(content='{"findings": []}'))
    return mock.Mock(choices=[choice], usage=usage)


class CompleteTests(unittest.TestCase):
    """The model call refuses anything that could have been reviewed from a cut-off prompt."""

    CONTENT = "DIFF:\n" + "+    var value = Compute(input, offset);\n" * 200

    def setUp(self):
        self.client = mock.Mock()
        for p in [mock.patch.object(llm, "_client", return_value=self.client),
                  mock.patch.object(llm, "_settings", return_value=(None, 0.6)),
                  mock.patch.object(config, "CONTEXT_TOKENS", 32768), mock.patch.object(config, "OUTPUT_TOKENS", 12288)]:
            self.enterContext(p)
        self.sent = llm.prompt_tokens("Review.", self.CONTENT)

    def call(self, response):
        self.client.chat.completions.create.return_value = response
        return llm.complete("m:1b", "Review.", self.CONTENT)

    def test_estimate_errs_high_for_code_and_counts_non_ascii_as_a_token_each(self):
        self.assertEqual(llm.estimated_tokens("x" * 2400), 1000)  # code runs ~2.4-2.7 chars/token, so this errs high
        self.assertEqual(llm.estimated_tokens("日本語のコメント"), 8)
        self.assertEqual(llm.estimated_tokens("café" * 600), 750 + 600)

    def test_a_reply_that_read_the_whole_prompt_is_kept(self):
        self.assertEqual(self.call(reply(prompt_tokens=int(self.sent * 0.85))), '{"findings": []}')
        self.assertEqual(self.call(reply()), '{"findings": []}')  # a server that reports no usage

    def test_running_out_of_reply_space_is_its_own_error_and_thinking_can_be_turned_off(self):
        with self.assertRaises(llm.OutputLimit):
            self.call(reply(finish="length"))
        sent = self.client.chat.completions.create.call_args.kwargs
        self.assertEqual(sent["temperature"], 0.6)  # the model's own setting from models.yaml
        self.assertNotIn("reasoning_effort", sent)
        self.client.chat.completions.create.return_value = reply()
        llm.complete("m:1b", "Review.", self.CONTENT, think=False)
        self.assertEqual(self.client.chat.completions.create.call_args.kwargs["reasoning_effort"], "none")

    def test_a_prompt_the_server_cut_short_is_a_failure(self):
        # Ollama with a too-small context keeps about half its window and still answers 200.
        with self.assertRaisesRegex(llm.LlmError, "cut the prompt off"):
            self.call(reply(prompt_tokens=int(self.sent * 0.15)))

    def test_prompt_and_reply_overflowing_the_window_is_a_failure(self):
        with self.assertRaisesRegex(llm.LlmError, "overflowed the 32768-token context"):
            self.call(reply(prompt_tokens=24_000, completion_tokens=9_000))

    def test_a_prompt_too_big_for_the_window_is_never_sent(self):
        with mock.patch.object(config, "CONTEXT_TOKENS", 13000):  # leaves 712 tokens for the prompt
            with self.assertRaisesRegex(llm.LlmError, "send a smaller diff"):
                llm.complete("m:1b", "Review.", self.CONTENT)
        self.client.chat.completions.create.assert_not_called()

    def test_an_unreachable_server_says_so(self):
        self.client.chat.completions.create.side_effect = openai.APIConnectionError(request=REQUEST)
        with self.assertRaises(llm.LlmUnavailable) as caught:
            llm.complete("m:1b", "Review.", self.CONTENT)
        self.assertIsInstance(caught.exception, models.ModelServerDown)

    def test_a_timeout_is_not_an_unreachable_server(self):
        self.client.chat.completions.create.side_effect = openai.APITimeoutError(request=REQUEST)
        with self.assertRaises(llm.LlmError) as caught:
            llm.complete("m:1b", "Review.", self.CONTENT)
        self.assertNotIsInstance(caught.exception, models.ModelServerDown)


if __name__ == "__main__":
    unittest.main()
