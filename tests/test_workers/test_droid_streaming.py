"""Tests for DroidWorker streaming functionality.

Tests JSONL event mapping to UniversalEvent types.
"""

import pytest
from pathlib import Path
from unittest.mock import MagicMock

from bender.workers.droid import DroidWorker
from bender.workers.base import WorkerConfig
# EventType stub
import enum
class EventType(str, enum.Enum):
    SESSION_START = "session.started"
    TASK_UPDATE = "task.updated"


class TestDroidWorkerCreateAdapter:
    """Tests for _create_streaming_adapter() method."""

    def test_create_streaming_adapter_returns_droid_adapter(self):
        config = WorkerConfig(project_path=Path("/tmp/test"))
        worker = DroidWorker(config=config)

        adapter = worker._create_streaming_adapter()

        assert adapter is not None
        assert adapter.agent_type == "droid"


class TestDroidWorkerMapSystemEvent:
    """Tests for mapping 'system' JSONL events."""

    def test_map_system_event_to_session_start(self):
        config = WorkerConfig(project_path=Path("/tmp/test"))
        worker = DroidWorker(config=config)
        worker._streaming_adapter = worker._create_streaming_adapter()
        worker._streaming_adapter.start_session(
            agent_id="test-droid",
            session_id="test-123"
        )

        json_event = {
            "type": "system",
            "model": "claude-opus-4.6",
            "tools": ["Execute", "Read", "Edit"]
        }

        event = worker._map_jsonl_to_event(json_event)

        assert event is not None
        assert event.event_type == EventType.SESSION_START
        assert event.data["model"] == "claude-opus-4.6"
        assert event.data["tools_count"] == 3
        assert event.data["tools"] == ["Execute", "Read", "Edit"]

    def test_map_system_event_synthetic_flag(self):
        config = WorkerConfig(project_path=Path("/tmp/test"))
        worker = DroidWorker(config=config)
        worker._streaming_adapter = worker._create_streaming_adapter()
        worker._streaming_adapter.start_session(
            agent_id="test-droid",
            session_id="test-123"
        )

        json_event = {"type": "system", "model": "test-model", "tools": []}

        event = worker._map_jsonl_to_event(json_event)

        assert event.synthetic is True


class TestDroidWorkerMapMessageEvent:
    """Tests for mapping 'message' JSONL events."""

    def test_map_assistant_message_to_item_delta(self):
        config = WorkerConfig(project_path=Path("/tmp/test"))
        worker = DroidWorker(config=config)
        worker._streaming_adapter = worker._create_streaming_adapter()
        worker._streaming_adapter.start_session(
            agent_id="test-droid",
            session_id="test-123"
        )

        json_event = {
            "type": "message",
            "role": "assistant",
            "text": "I am analyzing the code..."
        }

        event = worker._map_jsonl_to_event(json_event)

        assert event is not None
        assert event.event_type == EventType.ITEM_DELTA
        assert event.data["role"] == "assistant"
        assert event.data["text"] == "I am analyzing the code..."

    def test_map_user_message_ignored(self):
        config = WorkerConfig(project_path=Path("/tmp/test"))
        worker = DroidWorker(config=config)
        worker._streaming_adapter = worker._create_streaming_adapter()
        worker._streaming_adapter.start_session(
            agent_id="test-droid",
            session_id="test-123"
        )

        json_event = {
            "type": "message",
            "role": "user",
            "text": "Fix this bug"
        }

        event = worker._map_jsonl_to_event(json_event)

        assert event is None

    def test_map_short_assistant_message_ignored(self):
        """Messages shorter than 10 chars are ignored (technical noise)."""
        config = WorkerConfig(project_path=Path("/tmp/test"))
        worker = DroidWorker(config=config)
        worker._streaming_adapter = worker._create_streaming_adapter()
        worker._streaming_adapter.start_session(
            agent_id="test-droid",
            session_id="test-123"
        )

        json_event = {
            "type": "message",
            "role": "assistant",
            "text": "OK"
        }

        event = worker._map_jsonl_to_event(json_event)

        assert event is None

    def test_map_assistant_message_empty_text_ignored(self):
        config = WorkerConfig(project_path=Path("/tmp/test"))
        worker = DroidWorker(config=config)
        worker._streaming_adapter = worker._create_streaming_adapter()
        worker._streaming_adapter.start_session(
            agent_id="test-droid",
            session_id="test-123"
        )

        json_event = {
            "type": "message",
            "role": "assistant",
            "text": ""
        }

        event = worker._map_jsonl_to_event(json_event)

        assert event is None


class TestDroidWorkerMapToolCallEvent:
    """Tests for mapping 'tool_call' JSONL events."""

    def test_map_tool_call_to_item_start(self):
        config = WorkerConfig(project_path=Path("/tmp/test"))
        worker = DroidWorker(config=config)
        worker._streaming_adapter = worker._create_streaming_adapter()
        worker._streaming_adapter.start_session(
            agent_id="test-droid",
            session_id="test-123"
        )

        json_event = {
            "type": "tool_call",
            "toolName": "Execute",
            "parameters": {"command": "ls -la"}
        }

        event = worker._map_jsonl_to_event(json_event)

        assert event is not None
        assert event.event_type == EventType.ITEM_START
        assert event.data["toolName"] == "Execute"
        assert event.data["parameters"]["command"] == "ls -la"

    def test_map_tool_call_read_file(self):
        config = WorkerConfig(project_path=Path("/tmp/test"))
        worker = DroidWorker(config=config)
        worker._streaming_adapter = worker._create_streaming_adapter()
        worker._streaming_adapter.start_session(
            agent_id="test-droid",
            session_id="test-123"
        )

        json_event = {
            "type": "tool_call",
            "toolName": "Read",
            "parameters": {"file_path": "/tmp/test.py"}
        }

        event = worker._map_jsonl_to_event(json_event)

        assert event is not None
        assert event.event_type == EventType.ITEM_START
        assert event.data["toolName"] == "Read"
        assert event.data["parameters"]["file_path"] == "/tmp/test.py"


class TestDroidWorkerMapToolResultEvent:
    """Tests for mapping 'tool_result' JSONL events."""

    def test_map_tool_result_success_to_item_end(self):
        config = WorkerConfig(project_path=Path("/tmp/test"))
        worker = DroidWorker(config=config)
        worker._streaming_adapter = worker._create_streaming_adapter()
        worker._streaming_adapter.start_session(
            agent_id="test-droid",
            session_id="test-123"
        )

        json_event = {
            "type": "tool_result",
            "value": "Command executed successfully",
            "isError": False
        }

        event = worker._map_jsonl_to_event(json_event)

        assert event is not None
        assert event.event_type == EventType.ITEM_END
        assert event.data["value"] == "Command executed successfully"
        assert event.data["isError"] is False

    def test_map_tool_result_error_to_item_end(self):
        config = WorkerConfig(project_path=Path("/tmp/test"))
        worker = DroidWorker(config=config)
        worker._streaming_adapter = worker._create_streaming_adapter()
        worker._streaming_adapter.start_session(
            agent_id="test-droid",
            session_id="test-123"
        )

        json_event = {
            "type": "tool_result",
            "value": "File not found",
            "isError": True
        }

        event = worker._map_jsonl_to_event(json_event)

        assert event is not None
        assert event.event_type == EventType.ITEM_END
        assert event.data["value"] == "File not found"
        assert event.data["isError"] is True


class TestDroidWorkerMapCompletionEvent:
    """Tests for mapping 'completion' JSONL events."""

    def test_map_completion_to_task_completed(self):
        config = WorkerConfig(project_path=Path("/tmp/test"))
        worker = DroidWorker(config=config)
        worker._streaming_adapter = worker._create_streaming_adapter()
        worker._streaming_adapter.start_session(
            agent_id="test-droid",
            session_id="test-123"
        )

        json_event = {
            "type": "completion",
            "finalText": "Task completed successfully",
            "durationMs": 5432,
            "numTurns": 3
        }

        event = worker._map_jsonl_to_event(json_event)

        assert event is not None
        assert event.event_type == EventType.TASK_COMPLETED
        assert event.data["finalText"] == "Task completed successfully"
        assert event.data["durationMs"] == 5432
        assert event.data["numTurns"] == 3


class TestDroidWorkerMapUnknownEvent:
    """Tests for handling unknown JSONL events."""

    def test_map_unknown_event_type_returns_none(self):
        config = WorkerConfig(project_path=Path("/tmp/test"))
        worker = DroidWorker(config=config)
        worker._streaming_adapter = worker._create_streaming_adapter()
        worker._streaming_adapter.start_session(
            agent_id="test-droid",
            session_id="test-123"
        )

        json_event = {
            "type": "unknown_type",
            "data": "some data"
        }

        event = worker._map_jsonl_to_event(json_event)

        assert event is None

    def test_map_event_missing_type_returns_none(self):
        config = WorkerConfig(project_path=Path("/tmp/test"))
        worker = DroidWorker(config=config)
        worker._streaming_adapter = worker._create_streaming_adapter()
        worker._streaming_adapter.start_session(
            agent_id="test-droid",
            session_id="test-123"
        )

        json_event = {"data": "some data"}

        event = worker._map_jsonl_to_event(json_event)

        assert event is None


class TestDroidWorkerEventSequence:
    """Tests for sequence numbers in mapped events."""

    def test_mapped_events_have_incrementing_sequence(self):
        config = WorkerConfig(project_path=Path("/tmp/test"))
        worker = DroidWorker(config=config)
        worker._streaming_adapter = worker._create_streaming_adapter()
        worker._streaming_adapter.start_session(
            agent_id="test-droid",
            session_id="test-123"
        )

        event1 = worker._map_jsonl_to_event({
            "type": "system",
            "model": "test",
            "tools": []
        })

        event2 = worker._map_jsonl_to_event({
            "type": "message",
            "role": "assistant",
            "text": "Hello world test"
        })

        event3 = worker._map_jsonl_to_event({
            "type": "tool_call",
            "toolName": "Test",
            "parameters": {}
        })

        # Sequence should increment
        assert event1.sequence < event2.sequence
        assert event2.sequence < event3.sequence


class TestDroidWorkerAgentMetadata:
    """Tests for agent metadata in mapped events."""

    def test_mapped_events_have_correct_agent_type(self):
        config = WorkerConfig(project_path=Path("/tmp/test"))
        worker = DroidWorker(config=config)
        worker._streaming_adapter = worker._create_streaming_adapter()
        worker._streaming_adapter.start_session(
            agent_id="droid-worker-1",
            session_id="test-123"
        )

        event = worker._map_jsonl_to_event({
            "type": "system",
            "model": "test",
            "tools": []
        })

        assert event.agent_type == "droid"
        assert event.agent_id == "droid-worker-1"
        assert event.session_id == "test-123"


class TestDroidWorkerCompleteEventFlow:
    """Integration tests for complete event mapping flow."""

    def test_complete_droid_jsonl_flow(self):
        """Test mapping a complete sequence of JSONL events."""
        config = WorkerConfig(project_path=Path("/tmp/test"))
        worker = DroidWorker(config=config)
        worker._streaming_adapter = worker._create_streaming_adapter()
        worker._streaming_adapter.start_session(
            agent_id="droid-w1",
            session_id="test-session"
        )

        # System initialization
        event1 = worker._map_jsonl_to_event({
            "type": "system",
            "model": "claude-opus-4.6",
            "tools": ["Execute", "Read"]
        })
        assert event1.event_type == EventType.SESSION_START

        # Assistant message
        event2 = worker._map_jsonl_to_event({
            "type": "message",
            "role": "assistant",
            "text": "I will execute the command"
        })
        assert event2.event_type == EventType.ITEM_DELTA

        # Tool call
        event3 = worker._map_jsonl_to_event({
            "type": "tool_call",
            "toolName": "Execute",
            "parameters": {"command": "pytest"}
        })
        assert event3.event_type == EventType.ITEM_START

        # Tool result
        event4 = worker._map_jsonl_to_event({
            "type": "tool_result",
            "value": "All tests passed",
            "isError": False
        })
        assert event4.event_type == EventType.ITEM_END

        # Completion
        event5 = worker._map_jsonl_to_event({
            "type": "completion",
            "finalText": "Task completed",
            "durationMs": 1000,
            "numTurns": 1
        })
        assert event5.event_type == EventType.TASK_COMPLETED

        # Verify all events are from same session
        assert all(
            e.session_id == "test-session"
            for e in [event1, event2, event3, event4, event5]
        )


class TestDroidWorkerMapNoneAdapter:
    """Test _map_jsonl_to_event guard when adapter is None."""

    def test_map_returns_none_when_adapter_not_initialized(self):
        config = WorkerConfig(project_path=Path("/tmp/test"))
        worker = DroidWorker(config=config)
        # adapter is None before event_stream()
        assert worker._streaming_adapter is None

        result = worker._map_jsonl_to_event({"type": "system", "model": "test"})
        assert result is None


class TestDroidWorkerEventStreamIntegration:
    """Integration test: event_stream with real log file."""

    @pytest.mark.asyncio
    async def test_event_stream_reads_jsonl_log(self, tmp_path):
        """Simulate incremental JSONL log writes and verify event stream."""
        import asyncio
        import json

        log_file = tmp_path / "test-droid.log"
        done_file = tmp_path / "test-droid.done"

        # Write JSONL events to log file
        jsonl_events = [
            {"type": "system", "model": "claude-4", "tools": ["bash", "read"]},
            {"type": "message", "role": "assistant", "text": "Working on the task now with full details"},
            {"type": "tool_call", "toolName": "Bash", "parameters": {"command": "ls"}},
            {"type": "tool_result", "value": "file1.py\nfile2.py", "isError": False},
            {"type": "completion", "finalText": "Done", "durationMs": 500, "numTurns": 1},
        ]
        log_content = "\n".join(json.dumps(e) for e in jsonl_events) + "\n"
        log_file.write_text(log_content)
        done_file.write_text("0")  # exit code 0

        config = WorkerConfig(project_path=tmp_path)
        worker = DroidWorker(config=config)
        worker._log_file = log_file
        worker.session_id = "integration-test"
        worker.status = MagicMock()
        worker.status.__eq__ = lambda s, o: True  # Always "RUNNING"

        # Mock is_session_alive to return False after first iteration
        call_count = 0

        async def mock_alive():
            nonlocal call_count
            call_count += 1
            return call_count < 2

        worker.is_session_alive = mock_alive

        events = []
        async for event in worker.event_stream():
            events.append(event)
            if event.event_type == EventType.SESSION_END:
                break

        # Verify event sequence
        event_types = [e.event_type for e in events]
        assert EventType.SESSION_START in event_types
        assert EventType.ITEM_DELTA in event_types
        assert EventType.TASK_COMPLETED in event_types
        assert event_types[-1] == EventType.SESSION_END
