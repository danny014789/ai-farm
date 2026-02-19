"""Tests for bot.handlers helper functions."""

import sys
from unittest.mock import MagicMock

# bot.handlers imports telegram which isn't installed locally.
# Stub the module so we can import the pure-Python helpers.
_telegram_mock = MagicMock()
sys.modules.setdefault("telegram", _telegram_mock)
sys.modules.setdefault("telegram.ext", _telegram_mock)
sys.modules.setdefault("bot.keyboards", MagicMock())

from bot.handlers import _split_text, TELEGRAM_MAX_LENGTH


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
