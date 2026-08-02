"""Tests for bot.handlers helper functions."""

import sys
from unittest.mock import MagicMock

# bot.handlers imports telegram which isn't installed locally.
# Stub the module so we can import the pure-Python helpers.
_telegram_mock = MagicMock()
sys.modules.setdefault("telegram", _telegram_mock)
sys.modules.setdefault("telegram.ext", _telegram_mock)
sys.modules.setdefault("bot.keyboards", MagicMock())

import asyncio
import types

import pytest

import bot.handlers as handlers
from bot.handlers import _split_text, TELEGRAM_MAX_LENGTH
from src.claude_client import MODEL


class TestSplitText:
    """Tests for _split_text()."""

    def test_short_message_returns_single_chunk(self):
        text = "Hello, how is my plant?"
        chunks = _split_text(text)
        assert chunks == [text]

    def test_empty_string(self):
        assert _split_text("") == [""]

    def test_exact_max_length(self):
        text = "a" * TELEGRAM_MAX_LENGTH
        chunks = _split_text(text)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_splits_at_newline_boundaries(self):
        # Build text that requires splitting: two blocks that together exceed max
        block_a = "Line A\n" * 300  # ~2100 chars
        block_b = "Line B\n" * 400  # ~2800 chars
        text = block_a + block_b
        assert len(text) > TELEGRAM_MAX_LENGTH

        chunks = _split_text(text)
        assert len(chunks) >= 2
        # Each chunk should be within the limit
        for chunk in chunks:
            assert len(chunk) <= TELEGRAM_MAX_LENGTH

    def test_content_preservation(self):
        """All original content must be recoverable from chunks."""
        lines = [f"Line {i}: some content here" for i in range(300)]
        text = "\n".join(lines)
        chunks = _split_text(text)

        reassembled = "\n".join(chunks)
        assert reassembled == text

    def test_hard_split_for_long_line(self):
        """A single line longer than max_length gets hard-split."""
        long_line = "x" * 5000
        chunks = _split_text(long_line, max_length=2000)
        assert len(chunks) == 3  # 2000 + 2000 + 1000
        assert chunks[0] == "x" * 2000
        assert chunks[1] == "x" * 2000
        assert chunks[2] == "x" * 1000

    def test_custom_max_length(self):
        text = "aaaa\nbbbb\ncccc"
        chunks = _split_text(text, max_length=9)
        # "aaaa\nbbbb" = 9 chars, "cccc" = 4 chars
        assert chunks == ["aaaa\nbbbb", "cccc"]

    def test_mixed_short_and_long_lines(self):
        """Mix of normal lines and one very long line."""
        lines = ["short"] * 5 + ["x" * 100] + ["short"] * 5
        text = "\n".join(lines)
        chunks = _split_text(text, max_length=50)
        # Each chunk respects max_length
        for chunk in chunks:
            assert len(chunk) <= 100
        # All text content is present across chunks
        all_content = "".join(chunks)
        assert all_content.count("short") == 10
        assert "x" * 100 in all_content


class TestResearchPlant:
    """Tests for _research_plant(), the /setplant research step.

    This path shipped broken once: handlers referenced MODEL without
    importing it, so every /setplant raised NameError and the user saw
    "Research failed: name 'MODEL' is not defined". Nothing in the suite
    executed the function, so 219 tests passed over the bug. These tests
    drive it for real, stubbing only the Anthropic network call.
    """

    @staticmethod
    def _run(tmp_path, monkeypatch, *, api_key="sk-test", text="Guide.\n"):
        """Drive _research_plant and return (replies, captured_kwargs)."""
        import anthropic

        captured = {}

        class FakeMessages:
            def create(self, **kwargs):
                captured.update(kwargs)
                block = types.SimpleNamespace(text=text)
                return types.SimpleNamespace(content=[block])

        class FakeAnthropic:
            def __init__(self, *a, **kw):
                self.messages = FakeMessages()

        monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)

        profile = {"plant": {"name": "basil"}}
        monkeypatch.setattr(handlers, "load_plant_profile", lambda: profile)
        monkeypatch.setattr(handlers, "save_plant_profile", lambda p: profile.update(p))

        replies = []

        async def reply_text(t):
            replies.append(t)

        query = types.SimpleNamespace(
            message=types.SimpleNamespace(reply_text=reply_text)
        )
        context = types.SimpleNamespace(
            bot_data={"anthropic_api_key": api_key, "data_dir": str(tmp_path)}
        )

        asyncio.run(handlers._research_plant(query, context, "basil", "seedling"))
        return replies, captured, profile

    def test_uses_shared_model_constant(self, tmp_path, monkeypatch):
        """Regression: MODEL must be in scope, and must be the shared value."""
        replies, captured, _ = self._run(tmp_path, monkeypatch)
        assert captured["model"] == MODEL

    def test_reports_success_not_failure(self, tmp_path, monkeypatch):
        """The NameError surfaced here as a 'Research failed' reply."""
        replies, _, _ = self._run(tmp_path, monkeypatch)
        assert "Research complete" in replies[0]
        assert "Research failed" not in replies[0]
        assert "not defined" not in replies[0]

    def test_writes_knowledge_file(self, tmp_path, monkeypatch):
        self._run(tmp_path, monkeypatch, text="Basil likes warmth.\n")
        knowledge = tmp_path / "plant_knowledge.md"
        assert knowledge.exists()
        assert "Basil likes warmth." in knowledge.read_text()

    def test_parses_ideal_json_into_profile(self, tmp_path, monkeypatch):
        text = (
            "Care notes here.\n"
            'IDEAL_JSON: {"temp_min_c": 20, "temp_max_c": 28, "light_hours": 16}\n'
        )
        _, _, profile = self._run(tmp_path, monkeypatch, text=text)
        assert profile["ideal_conditions"]["temp_min_c"] == 20
        assert profile["ideal_conditions"]["light_hours"] == 16
        assert profile["knowledge_cached"] is True

    def test_malformed_ideal_json_does_not_crash(self, tmp_path, monkeypatch):
        """Bad JSON is logged and skipped, not raised at the user."""
        text = "Notes.\nIDEAL_JSON: {not valid json at all}\n"
        replies, _, _ = self._run(tmp_path, monkeypatch, text=text)
        assert "Research complete" in replies[0]

    def test_missing_api_key_short_circuits(self, tmp_path, monkeypatch):
        replies, captured, _ = self._run(tmp_path, monkeypatch, api_key="")
        assert "not configured" in replies[0]
        assert captured == {}, "must not call the API without a key"
