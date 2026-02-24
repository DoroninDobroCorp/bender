"""Tests for CodexWorker streaming functionality.

Tests GenericStreamingAdapter integration for codex worker.
"""

import pytest
from pathlib import Path

from bender.workers.codex import CodexWorker
from bender.workers.base import WorkerConfig, GenericStreamingAdapter


class TestCodexWorkerCreateAdapter:
    """Tests for _create_streaming_adapter() method."""

    def test_create_streaming_adapter_returns_generic_adapter(self):
        config = WorkerConfig(project_path=Path("/tmp/test"))
        worker = CodexWorker(config=config)

        adapter = worker._create_streaming_adapter()

        assert adapter is not None
        assert isinstance(adapter, GenericStreamingAdapter)

    def test_created_adapter_has_correct_agent_type(self):
        config = WorkerConfig(project_path=Path("/tmp/test"))
        worker = CodexWorker(config=config)

        adapter = worker._create_streaming_adapter()

        assert adapter.agent_type == "codex"

    def test_adapter_can_start_session(self):
        config = WorkerConfig(project_path=Path("/tmp/test"))
        worker = CodexWorker(config=config)
        adapter = worker._create_streaming_adapter()

        event = adapter.start_session(
            agent_id="codex-w1",
            session_id="test-123"
        )

        assert event is not None
        assert event.agent_type == "codex"
        assert event.agent_id == "codex-w1"

    def test_adapter_can_feed_text(self):
        config = WorkerConfig(project_path=Path("/tmp/test"))
        worker = CodexWorker(config=config)
        adapter = worker._create_streaming_adapter()

        adapter.start_session(agent_id="codex-w1")
        events = adapter.feed_text("Test output from codex")

        assert len(events) == 2  # ITEM_START + ITEM_DELTA
        assert events[1].data["text"] == "Test output from codex"

    def test_adapter_can_end_session(self):
        config = WorkerConfig(project_path=Path("/tmp/test"))
        worker = CodexWorker(config=config)
        adapter = worker._create_streaming_adapter()

        adapter.start_session(agent_id="codex-w1")
        adapter.feed_text("Some text")
        events = adapter.end_session()

        assert len(events) == 2  # ITEM_END + SESSION_END


class TestCodexWorkerAdapterIntegration:
    """Integration tests for CodexWorker adapter usage."""

    def test_multiple_workers_have_independent_adapters(self):
        config = WorkerConfig(project_path=Path("/tmp/test"))
        worker1 = CodexWorker(config=config)
        worker2 = CodexWorker(config=config)

        adapter1 = worker1._create_streaming_adapter()
        adapter2 = worker2._create_streaming_adapter()

        # Adapters should be independent instances
        adapter1.start_session(agent_id="codex-w1")
        adapter2.start_session(agent_id="codex-w2")

        # Each adapter tracks its own sequence
        events1 = adapter1.feed_text("Worker 1 text")
        events2 = adapter2.feed_text("Worker 2 text")

        assert events1[0].agent_id == "codex-w1"
        assert events2[0].agent_id == "codex-w2"

    def test_adapter_persists_across_calls(self):
        """Test that created adapter can be reused."""
        config = WorkerConfig(project_path=Path("/tmp/test"))
        worker = CodexWorker(config=config)

        adapter = worker._create_streaming_adapter()

        # Use adapter multiple times
        adapter.start_session(agent_id="codex-w1")
        adapter.feed_text("Chunk 1")
        adapter.feed_text("Chunk 2")
        adapter.feed_text("Chunk 3")
        end_events = adapter.end_session()

        assert end_events[-1].sequence > 0  # Sequence should have incremented


class TestCodexWorkerAdapterSyntheticEvents:
    """Tests for synthetic event generation."""

    def test_all_events_are_synthetic(self):
        config = WorkerConfig(project_path=Path("/tmp/test"))
        worker = CodexWorker(config=config)
        adapter = worker._create_streaming_adapter()

        start_event = adapter.start_session(agent_id="codex-w1")
        delta_events = adapter.feed_text("Test text")
        end_events = adapter.end_session()

        assert start_event.synthetic is True
        assert all(e.synthetic is True for e in delta_events)
        assert all(e.synthetic is True for e in end_events)

    def test_events_have_daemon_source(self):
        config = WorkerConfig(project_path=Path("/tmp/test"))
        worker = CodexWorker(config=config)
        adapter = worker._create_streaming_adapter()

        start_event = adapter.start_session(agent_id="codex-w1")
        delta_events = adapter.feed_text("Test text")
        end_events = adapter.end_session()

        # EventSource stub
import enum
class EventSource(str, enum.Enum):
    AGENT = "agent"

        assert start_event.source == EventSource.DAEMON
        assert all(e.source == EventSource.DAEMON for e in delta_events)
        assert all(e.source == EventSource.DAEMON for e in end_events)


class TestCodexWorkerAdapterComparison:
    """Tests comparing codex adapter with copilot adapter."""

    def test_codex_and_copilot_adapters_are_independent(self):
        """Verify that codex and copilot use separate adapter instances."""
        from bender.workers.copilot import CopilotWorker

        config = WorkerConfig(project_path=Path("/tmp/test"))
        codex_worker = CodexWorker(config=config)
        copilot_worker = CopilotWorker(config=config)

        codex_adapter = codex_worker._create_streaming_adapter()
        copilot_adapter = copilot_worker._create_streaming_adapter()

        # Both should be GenericStreamingAdapter but different instances
        assert isinstance(codex_adapter, GenericStreamingAdapter)
        assert isinstance(copilot_adapter, GenericStreamingAdapter)
        assert codex_adapter is not copilot_adapter

        # Should have different agent types
        assert codex_adapter.agent_type == "codex"
        assert copilot_adapter.agent_type == "copilot"

    def test_codex_events_have_codex_agent_type(self):
        """Verify that events from codex adapter have correct agent_type."""
        config = WorkerConfig(project_path=Path("/tmp/test"))
        worker = CodexWorker(config=config)
        adapter = worker._create_streaming_adapter()

        event = adapter.start_session(agent_id="codex-w1")

        assert event.agent_type == "codex"
        assert event.agent_id == "codex-w1"
