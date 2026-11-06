"""ai.py talks to OpenRouter or Anthropic in the same Messages format."""
import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ai  # noqa: E402


def load(env):
    keys = ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "AI_PROVIDER", "AI_AUDIT_MODEL", "AI_AGENT_MODEL",
            "ANTHROPIC_AUDIT_MODEL", "ANTHROPIC_AGENT_MODEL")
    clean = {k: "" for k in keys}
    with mock.patch.dict(os.environ, {**clean, **env}):
        with mock.patch("dotenv.load_dotenv", return_value=False):
            return importlib.reload(ai), dict(os.environ)


class Ok:
    ok, status_code = True, 200

    def json(self):
        return {"content": [], "stop_reason": "end_turn"}


class Providers(unittest.TestCase):
    def tearDown(self):
        importlib.reload(ai)

    def call(self, env):
        mod, environ = load(env)
        with mock.patch.dict(os.environ, environ), mock.patch.object(mod.requests, "post", return_value=Ok()) as post:
            mod.messages(model=mod.AUDIT_MODEL, max_tokens=10, messages=[])
        return mod, post.call_args

    def test_openrouter(self):
        mod, call = self.call({"OPENROUTER_API_KEY": "or-key"})
        self.assertEqual(mod.PROVIDER, "openrouter")
        self.assertEqual(call.args[0], "https://openrouter.ai/api/v1/messages")
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer or-key")
        self.assertNotIn("x-api-key", call.kwargs["headers"])
        self.assertEqual(call.kwargs["json"]["model"], "anthropic/claude-haiku-5.5")
        self.assertEqual(mod.AGENT_MODEL, "anthropic/claude-sonnet-5.5")

    def test_anthropic(self):
        mod, call = self.call({"ANTHROPIC_API_KEY": "sk-ant"})
        self.assertEqual(call.args[0], "https://api.anthropic.com/v1/messages")
        self.assertEqual(call.kwargs["headers"]["x-api-key"], "sk-ant")
        self.assertEqual(call.kwargs["json"]["model"], "claude-haiku-5-5")

    def test_openrouter_wins_unless_forced(self):
        self.assertEqual(load({"OPENROUTER_API_KEY": "a", "ANTHROPIC_API_KEY": "b"})[0].PROVIDER, "openrouter")
        self.assertEqual(load({"OPENROUTER_API_KEY": "a", "ANTHROPIC_API_KEY": "b", "AI_PROVIDER": "anthropic"})[0].PROVIDER, "anthropic")

    def test_model_override_and_no_key(self):
        mod, environ = load({"OPENROUTER_API_KEY": "", "AI_AUDIT_MODEL": "anthropic/claude-sonnet-5.5"})
        self.assertEqual(mod.AUDIT_MODEL, "anthropic/claude-sonnet-5.5")
        with mock.patch.dict(os.environ, environ):
            self.assertFalse(mod.available())
            out = mod.audit_basket("veg", [{"name": "Kale", "qty": 1, "price": 1}], 1, "Greenleaf")
        self.assertEqual(out["verdict"], "hold")  # no key: fail closed


if __name__ == "__main__":
    unittest.main()
