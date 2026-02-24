"""Tests for LogWatcher._safe_read_ndjson_lines() — AC-6.

Защита от race conditions при параллельном чтении/записи NDJSON файлов.
"""

import json
from unittest.mock import MagicMock

import pytest

from bender.log_watcher import LogWatcher


@pytest.fixture
def watcher() -> LogWatcher:
    """LogWatcher с mock LLM клиентом."""
    mock_glm = MagicMock()
    return LogWatcher(glm_client=mock_glm)


class TestSafeReadNdjsonLines:
    """Тесты для _safe_read_ndjson_lines()."""

    def test_valid_lines_returned(self, watcher: LogWatcher) -> None:
        """Полные валидные JSON строки возвращаются без изменений."""
        content = '{"type": "text", "text": "hello"}\n{"type": "end", "code": 0}'
        lines = watcher._safe_read_ndjson_lines(content)
        assert len(lines) == 2
        # Можно распарсить обратно
        obj1 = json.loads(lines[0])
        obj2 = json.loads(lines[1])
        assert obj1["type"] == "text"
        assert obj2["code"] == 0

    def test_truncated_last_line_ignored(self, watcher: LogWatcher) -> None:
        """Битая (недозаписанная) последняя строка игнорируется."""
        content = '{"type": "text", "text": "ok"}\n{"truncated":'
        lines = watcher._safe_read_ndjson_lines(content)
        assert len(lines) == 1
        assert json.loads(lines[0])["type"] == "text"

    def test_empty_content_returns_empty(self, watcher: LogWatcher) -> None:
        """Пустое содержимое → пустой список."""
        lines = watcher._safe_read_ndjson_lines("")
        assert lines == []

    def test_only_whitespace_ignored(self, watcher: LogWatcher) -> None:
        """Пустые строки / пробелы игнорируются."""
        content = "\n\n  \n"
        lines = watcher._safe_read_ndjson_lines(content)
        assert lines == []

    def test_multiple_valid_lines(self, watcher: LogWatcher) -> None:
        """Несколько полных строк — все возвращаются."""
        events = [
            {"type": "text", "text": f"line {i}"}
            for i in range(5)
        ]
        content = "\n".join(json.dumps(e) for e in events)
        lines = watcher._safe_read_ndjson_lines(content)
        assert len(lines) == 5

    def test_invalid_json_middle_line_ignored(self, watcher: LogWatcher) -> None:
        """Битая строка в середине файла также игнорируется."""
        content = '{"ok": 1}\n{broken json}\n{"ok": 2}'
        lines = watcher._safe_read_ndjson_lines(content)
        # Битая строка игнорируется, остальные валидны
        assert len(lines) == 2
        assert json.loads(lines[0])["ok"] == 1
        assert json.loads(lines[1])["ok"] == 2
