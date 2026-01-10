"""
Unit tests for bender module
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Dict, Any

from bender.base_client import BaseLLMClient, LLMProvider, LLMResponse
from bender.utils import parse_json_response, JSONParseError
from bender.watchdog import Watchdog, HealthStatus


class TestParseJsonResponse:
    """Tests for JSON parsing utility"""
    
    def test_parse_valid_json(self):
        """Should parse valid JSON"""
        result = parse_json_response('{"key": "value"}')
        assert result == {"key": "value"}
    
    def test_parse_json_with_markdown(self):
        """Should extract JSON from markdown code blocks"""
        text = '```json\n{"key": "value"}\n```'
        result = parse_json_response(text)
        assert result == {"key": "value"}
    
    def test_parse_json_with_surrounding_text(self):
        """Should extract JSON from text with surrounding content"""
        text = 'Here is the result: {"key": "value"} and more text'
        result = parse_json_response(text)
        assert result == {"key": "value"}
    
    def test_parse_empty_string_raises(self):
        """Should raise JSONParseError for empty string"""
        with pytest.raises(JSONParseError) as exc_info:
            parse_json_response('')
        assert exc_info.value.raw_text == ''
    
    def test_parse_invalid_json_raises(self):
        """Should raise JSONParseError for invalid JSON"""
        with pytest.raises(JSONParseError):
            parse_json_response('not json at all')
    
    def test_json_parse_error_preserves_raw_text(self):
        """JSONParseError should preserve the raw text"""
        raw = 'invalid json content'
        with pytest.raises(JSONParseError) as exc_info:
            parse_json_response(raw)
        assert exc_info.value.raw_text == raw


class TestWatchdog:
    """Tests for Watchdog health monitoring"""
    
    def test_watchdog_initialization(self):
        """Should initialize watchdog"""
        watchdog = Watchdog()
        assert watchdog is not None
    
    def test_watchdog_has_error_patterns(self):
        """Should have error patterns list"""
        watchdog = Watchdog()
        assert hasattr(watchdog, 'error_patterns')
    
    def test_check_health_returns_health_check(self):
        """Should return HealthCheck object"""
        watchdog = Watchdog()
        health = watchdog.check_health("test output", is_session_alive=True)
        assert hasattr(health, 'status')
        assert isinstance(health.status, HealthStatus)
    
    def test_check_health_with_invalid_regex(self):
        """Should handle invalid regex patterns gracefully"""
        watchdog = Watchdog()
        watchdog.error_patterns = ['[invalid']  # Invalid regex
        # Should not crash
        health = watchdog.check_health("test output", is_session_alive=True)
        assert health.status == HealthStatus.HEALTHY


class TestLLMProvider:
    """Tests for LLM provider enum"""
    
    def test_provider_values(self):
        """Should have correct provider values"""
        assert LLMProvider.GEMINI.value == "gemini"
        assert LLMProvider.GLM.value == "glm"


class TestLLMResponse:
    """Tests for LLMResponse dataclass"""
    
    def test_response_creation(self):
        """Should create response with all fields"""
        response = LLMResponse(
            content="test content",
            provider=LLMProvider.GEMINI,
            model="gemini-2.5-pro",
            tokens_used=100,
            latency_ms=500.0
        )
        assert response.content == "test content"
        assert response.provider == LLMProvider.GEMINI
        assert response.model == "gemini-2.5-pro"
        assert response.tokens_used == 100
        assert response.latency_ms == 500.0
    
    def test_response_optional_fields(self):
        """Should allow optional fields to be None"""
        response = LLMResponse(
            content="test",
            provider=LLMProvider.GLM,
            model="qwen-3-32b"
        )
        assert response.tokens_used is None
        assert response.latency_ms is None
