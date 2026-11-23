import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from vader import config, models


def ok_load(*a, **k):
    return httpx.Response(200, request=httpx.Request("POST", "http://ollama"))


class OllamaLoadTests(unittest.TestCase):
    """With MODEL_RUNTIME=ollama, Vader loads one model at a time and checks the context it got."""

    def setUp(self):
        self.enterContext(mock.patch.object(config, "MODEL_RUNTIME", "ollama"))
        self.enterContext(mock.patch.object(config, "CONTEXT_TOKENS", 32768))

    def load(self, context):
        loaded = [{"name": "m:1b", "size_gb": 1, "vram_gb": 1, "context": context}]
        with mock.patch.object(models, "loaded", return_value=loaded), \
                mock.patch.object(models.httpx, "post", side_effect=ok_load):
            models.ensure_only("m:1b")

    def test_a_smaller_context_than_configured_is_refused(self):
        # Ollama would otherwise cut prompts off silently.
        with self.assertRaisesRegex(RuntimeError, "OLLAMA_CONTEXT_LENGTH=32768"):
            self.load(4096)

    def test_matching_or_unknown_context_is_fine(self):
        self.load(32768)
        self.load(None)  # older Ollama versions don't report it

    def test_loading_with_the_server_down_says_so(self):
        with mock.patch.object(models, "loaded", return_value=None), \
                mock.patch.object(models.httpx, "post", side_effect=httpx.ConnectError("refused")):
            with self.assertRaises(models.ModelServerDown):
                models.ensure_only("m:1b")

    def test_unloading_from_a_server_that_is_gone_is_not_an_error(self):
        with mock.patch.object(models.httpx, "post", side_effect=httpx.ConnectError("refused")):
            models.unload("m:1b")


class RuntimeTests(unittest.TestCase):
    """Any OpenAI-compatible server works; Ollama's own API is only used with MODEL_RUNTIME=ollama."""

    def test_the_model_list_comes_from_the_openai_api(self):
        listing = httpx.Response(200, json={"data": [{"id": "qwen/qwen3-14b"}, {"id": "gpt-oss:20b"}]},
                                 request=httpx.Request("GET", "http://lmstudio/v1/models"))
        with mock.patch.object(config, "LLM_BASE_URL", "http://lmstudio/v1"), \
             mock.patch.object(models.httpx, "get", return_value=listing) as get:
            self.assertEqual(models.available(), {"qwen/qwen3-14b", "gpt-oss:20b"})
        self.assertEqual(get.call_args.args[0], "http://lmstudio/v1/models")
        with mock.patch.object(models.httpx, "get", side_effect=httpx.ConnectError("refused")):
            self.assertIsNone(models.available())  # unreachable

    def test_a_generic_server_manages_loading_itself(self):
        with mock.patch.object(models.httpx, "post") as post, mock.patch.object(models.httpx, "get") as get:
            models.ensure_only("qwen/qwen3-14b")
            models.unload("qwen/qwen3-14b")
            self.assertIsNone(models.loaded())
        post.assert_not_called()
        get.assert_not_called()
        self.assertEqual(models.normalize("qwen/qwen3-14b"), "qwen/qwen3-14b")  # names are used as given


class ModelsFileTests(unittest.TestCase):
    """models.yaml: the allowed list, the default, per-validator models, reasoning effort, and temperature."""

    def test_reads_the_allowed_models(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "models.yaml"
            path.write_text("default: qwen3:8b\nvalidators:\n  QA: devstral\n  security: ''\nmodels:\n"
                            "  - {name: qwen3:8b, tier: baseline, temperature: 0.6}\n  - {name: devstral, enabled: true}\n"
                            "  - {name: gpt-oss:20b, reasoning_effort: LOW}\n  - {name: old:1b, enabled: false}\n",
                            encoding="utf-8")
            with mock.patch.object(config, "MODELS_FILE", path):
                default, entries = models.catalog()
                self.assertEqual(default, "qwen3:8b")
                self.assertEqual([e.name for e in entries], ["qwen3:8b", "devstral", "gpt-oss:20b", "old:1b"])
                self.assertEqual(models.assignments(), {"qa": "devstral"})  # empty means the active model
                with mock.patch.object(config, "MODEL_RUNTIME", "ollama"):  # Ollama reads no tag as :latest
                    self.assertEqual(models.assignments(), {"qa": "devstral:latest"})
                self.assertEqual((models.reasoning_effort("gpt-oss:20b"), models.reasoning_effort("qwen3:8b")),
                                 ("low", None))
                self.assertEqual((models.temperature("qwen3:8b"), models.temperature("devstral")), (0.6, 0.1))
                self.assertIsNone(models.check_allowed("devstral"))
                self.assertIn("disabled", models.check_allowed("old:1b"))
                self.assertIn("isn't in the allowed list", models.check_allowed("llama3:70b"))


if __name__ == "__main__":
    unittest.main()
