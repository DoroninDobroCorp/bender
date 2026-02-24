"""Tests for GenericStreamingAdapter (plain text workers: copilot, codex).

Tests streaming adapter that converts plain text output to UniversalEvent.
"""

import pytest

from bender.workers.base import GenericStreamingAdapter
# EventType/EventSource stubs for standalone testing
import enum
class EventType(str, enum.Enum):
    SESSION_START = "session.started"
    TASK_UPDATE = "task.updated"
class EventSource(str, enum.Enum):
    AGENT = "agent"


class TestGenericStreamingAdapterInit:
    """Tests for adapter initialization."""

    def test_creates_with_agent_type(self):
        adapter = GenericStreamingAdapter(agent_type="copilot")
        assert adapter.agent_type == "copilot"

    def test_initial_sequence_is_zero(self):
        adapter = GenericStreamingAdapter(agent_type="codex")
        assert adapter._sequence == 0

    def test_initial_item_closed(self):
        adapter = GenericStreamingAdapter(agent_type="copilot")
        assert adapter._item_open is False


class TestGenericStreamingAdapterStartSession:
    """Tests for start_session() method."""

    def test_start_session_returns_event(self):
        adapter = GenericStreamingAdapter(agent_type="copilot")
        event = adapter.start_session(
            agent_id="copilot-w1",
            session_id="test-session-123"
        )

        assert event.event_type == EventType.SESSION_START
        assert event.agent_id == "copilot-w1"
        assert event.agent_type == "copilot"
        assert event.session_id == "test-session-123"

    def test_start_session_resets_sequence(self):
        adapter = GenericStreamingAdapter(agent_type="copilot")
        adapter._sequence = 42  # Set non-zero
        event = adapter.start_session(agent_id="copilot-w1")

        assert event.sequence == 0
        assert adapter._sequence == 1  # Incremented after first event

    def test_start_session_sets_synthetic_flag(self):
        adapter = GenericStreamingAdapter(agent_type="copilot")
        event = adapter.start_session(agent_id="copilot-w1")

        assert event.synthetic is True
        assert event.source == EventSource.DAEMON

    def test_start_session_with_user_id(self):
        from uuid import uuid4
        user_id = uuid4()
        adapter = GenericStreamingAdapter(agent_type="copilot")
        event = adapter.start_session(
            agent_id="copilot-w1",
            user_id=user_id
        )

        assert event.user_id == user_id


class TestGenericStreamingAdapterFeedText:
    """Tests for feed_text() method."""

    def test_feed_text_first_call_returns_start_and_delta(self):
        adapter = GenericStreamingAdapter(agent_type="copilot")
        adapter.start_session(agent_id="copilot-w1")

        events = adapter.feed_text("First chunk")

        assert len(events) == 2
        assert events[0].event_type == EventType.ITEM_START
        assert events[1].event_type == EventType.ITEM_DELTA
        assert events[1].data["text"] == "First chunk"

    def test_feed_text_subsequent_returns_only_delta(self):
        adapter = GenericStreamingAdapter(agent_type="copilot")
        adapter.start_session(agent_id="copilot-w1")

        # First call
        adapter.feed_text("First chunk")

        # Second call
        events = adapter.feed_text("Second chunk")

        assert len(events) == 1
        assert events[0].event_type == EventType.ITEM_DELTA
        assert events[0].data["text"] == "Second chunk"

    def test_feed_text_empty_returns_empty_list(self):
        adapter = GenericStreamingAdapter(agent_type="copilot")
        adapter.start_session(agent_id="copilot-w1")

        events = adapter.feed_text("")

        assert events == []

    def test_feed_text_without_session_raises_error(self):
        adapter = GenericStreamingAdapter(agent_type="copilot")

        with pytest.raises(RuntimeError, match="Session not started"):
            adapter.feed_text("Some text")

    def test_feed_text_increments_sequence(self):
        adapter = GenericStreamingAdapter(agent_type="copilot")
        adapter.start_session(agent_id="copilot-w1")

        events = adapter.feed_text("First chunk")

        # SESSION_START = 0, ITEM_START = 1, ITEM_DELTA = 2
        assert events[0].sequence == 1  # ITEM_START
        assert events[1].sequence == 2  # ITEM_DELTA


class TestGenericStreamingAdapterEndSession:
    """Tests for end_session() method."""

    def test_end_session_with_open_item_returns_item_end_and_session_end(self):
        adapter = GenericStreamingAdapter(agent_type="copilot")
        adapter.start_session(agent_id="copilot-w1")
        adapter.feed_text("Some text")  # Opens item

        events = adapter.end_session()

        assert len(events) == 2
        assert events[0].event_type == EventType.ITEM_END
        assert events[1].event_type == EventType.SESSION_END

    def test_end_session_without_open_item_returns_only_session_end(self):
        adapter = GenericStreamingAdapter(agent_type="copilot")
        adapter.start_session(agent_id="copilot-w1")
        # No feed_text called

        events = adapter.end_session()

        assert len(events) == 1
        assert events[0].event_type == EventType.SESSION_END

    def test_end_session_clears_agent_id(self):
        adapter = GenericStreamingAdapter(agent_type="copilot")
        adapter.start_session(agent_id="copilot-w1")

        adapter.end_session()

        assert adapter._agent_id is None

    def test_end_session_without_session_raises_error(self):
        adapter = GenericStreamingAdapter(agent_type="copilot")

        with pytest.raises(RuntimeError, match="Session not started"):
            adapter.end_session()


class TestGenericStreamingAdapterSequenceNumbers:
    """Tests for sequence number generation."""

    def test_sequence_increments_correctly(self):
        adapter = GenericStreamingAdapter(agent_type="copilot")
        event1 = adapter.start_session(agent_id="copilot-w1")
        events2 = adapter.feed_text("First")
        events3 = adapter.feed_text("Second")
        events4 = adapter.end_session()

        # SESSION_START
        assert event1.sequence == 0

        # ITEM_START, ITEM_DELTA
        assert events2[0].sequence == 1
        assert events2[1].sequence == 2

        # ITEM_DELTA (only)
        assert events3[0].sequence == 3

        # ITEM_END, SESSION_END
        assert events4[0].sequence == 4
        assert events4[1].sequence == 5


class TestGenericStreamingAdapterAgentType:
    """Tests for agent_type in events."""

    @pytest.mark.parametrize("agent_type", ["copilot", "codex", "custom-agent"])
    def test_agent_type_propagated_to_events(self, agent_type: str):
        adapter = GenericStreamingAdapter(agent_type=agent_type)
        event = adapter.start_session(agent_id=f"{agent_type}-w1")

        assert event.agent_type == agent_type


class TestGenericStreamingAdapterSyntheticFlag:
    """Tests for synthetic flag in events."""

    def test_all_events_have_synthetic_true(self):
        adapter = GenericStreamingAdapter(agent_type="copilot")
        event1 = adapter.start_session(agent_id="copilot-w1")
        events2 = adapter.feed_text("Text")
        events3 = adapter.end_session()

        assert event1.synthetic is True
        assert all(e.synthetic is True for e in events2)
        assert all(e.synthetic is True for e in events3)

    def test_all_events_have_daemon_source(self):
        adapter = GenericStreamingAdapter(agent_type="copilot")
        event1 = adapter.start_session(agent_id="copilot-w1")
        events2 = adapter.feed_text("Text")
        events3 = adapter.end_session()

        assert event1.source == EventSource.DAEMON
        assert all(e.source == EventSource.DAEMON for e in events2)
        assert all(e.source == EventSource.DAEMON for e in events3)


class TestGenericStreamingAdapterDataField:
    """Tests for data field in events."""

    def test_feed_text_stores_text_in_data(self):
        adapter = GenericStreamingAdapter(agent_type="copilot")
        adapter.start_session(agent_id="copilot-w1")

        events = adapter.feed_text("Test text 123")

        delta_event = events[1]  # ITEM_DELTA
        assert delta_event.data["text"] == "Test text 123"

    def test_start_and_end_events_have_empty_data(self):
        adapter = GenericStreamingAdapter(agent_type="copilot")
        start_event = adapter.start_session(agent_id="copilot-w1")
        end_events = adapter.end_session()

        assert start_event.data == {}
        assert all(e.data == {} for e in end_events)


class TestGenericStreamingAdapterCompleteFlow:
    """Integration tests for complete streaming flow."""

    def test_complete_streaming_flow(self):
        adapter = GenericStreamingAdapter(agent_type="copilot")

        # Start session
        event1 = adapter.start_session(
            agent_id="copilot-worker-1",
            session_id="test-session"
        )
        assert event1.event_type == EventType.SESSION_START
        assert event1.sequence == 0

        # Feed first chunk (opens item)
        events2 = adapter.feed_text("First chunk of text")
        assert len(events2) == 2
        assert events2[0].event_type == EventType.ITEM_START
        assert events2[1].event_type == EventType.ITEM_DELTA
        assert events2[1].data["text"] == "First chunk of text"

        # Feed more chunks
        events3 = adapter.feed_text("Second chunk")
        assert len(events3) == 1
        assert events3[0].event_type == EventType.ITEM_DELTA

        events4 = adapter.feed_text("Third chunk")
        assert len(events4) == 1

        # End session
        events5 = adapter.end_session()
        assert len(events5) == 2
        assert events5[0].event_type == EventType.ITEM_END
        assert events5[1].event_type == EventType.SESSION_END

    def test_empty_session_flow(self):
        """Test session with no text fed."""
        adapter = GenericStreamingAdapter(agent_type="codex")

        start_event = adapter.start_session(agent_id="codex-w1")
        assert start_event.event_type == EventType.SESSION_START

        end_events = adapter.end_session()
        assert len(end_events) == 1
        assert end_events[0].event_type == EventType.SESSION_END

    def test_multiple_items_in_session(self):
        """Test closing and opening items within session."""
        adapter = GenericStreamingAdapter(agent_type="copilot")

        adapter.start_session(agent_id="copilot-w1")

        # First item
        events1 = adapter.feed_text("Item 1 text")
        assert events1[0].event_type == EventType.ITEM_START
        assert adapter._item_open is True

        # Manually close item (через end_session)
        # Note: GenericStreamingAdapter не поддерживает явное закрытие item,
        # но это тест что item остаётся открытым пока не вызван end_session
        events2 = adapter.feed_text("More text")
        assert len(events2) == 1  # Only delta, item still open

        end_events = adapter.end_session()
        assert end_events[0].event_type == EventType.ITEM_END


class TestGenericStreamingAdapterEdgeCases:
    """Edge case tests for robustness."""

    def test_feed_text_whitespace_only_returns_empty(self):
        adapter = GenericStreamingAdapter(agent_type="test")
        adapter.start_session(agent_id="test-w1")

        events = adapter.feed_text("   \n  \t  ")
        assert events == []

    def test_feed_text_truncates_large_chunks(self):
        adapter = GenericStreamingAdapter(agent_type="test")
        adapter.start_session(agent_id="test-w1")

        # Create text larger than MAX_TEXT_CHUNK_SIZE (10 MB)
        huge_text = "x" * (adapter.MAX_TEXT_CHUNK_SIZE + 1000)
        events = adapter.feed_text(huge_text)

        # Should still produce events (truncated)
        assert len(events) == 2  # ITEM_START + ITEM_DELTA
        delta = events[1]
        assert len(delta.data["text"]) == adapter.MAX_TEXT_CHUNK_SIZE
