"""
Unit tests for core module
"""

import pytest
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from core.config import Config
from core.exceptions import (
    ParserMakerError,
    LLMConnectionError,
    LLMResponseError,
    JSONParseError,
    DroidError,
    PipelineError,
    StepError
)


class TestConfig:
    """Tests for Config class"""
    
    def test_config_from_env(self):
        """Should load config from environment"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict('os.environ', {
                'GLM_API_KEY': 'test_glm_key',
                'GEMINI_API_KEY': 'test_gemini_key',
                'DROID_PROJECT_PATH': tmpdir,
            }):
                config = Config()
                assert config.glm_api_key == 'test_glm_key'
    
    def test_config_defaults(self):
        """Should have sensible defaults"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict('os.environ', {
                'GLM_API_KEY': 'test_key',
                'DROID_PROJECT_PATH': tmpdir,
            }, clear=False):
                config = Config()
                assert config.droid_binary == 'droid'
                assert config.bender_escalate_after == 5


class TestExceptions:
    """Tests for custom exceptions"""
    
    def test_parser_maker_error(self):
        """Should create base error"""
        error = ParserMakerError("test error")
        assert str(error) == "test error"
    
    def test_llm_connection_error(self):
        """Should create LLM connection error"""
        error = LLMConnectionError("connection failed")
        assert isinstance(error, ParserMakerError)
        assert "connection failed" in str(error)
    
    def test_llm_response_error(self):
        """Should create LLM response error"""
        error = LLMResponseError("invalid response")
        assert isinstance(error, ParserMakerError)
    
    def test_json_parse_error_with_raw_text(self):
        """Should preserve raw text in JSONParseError"""
        raw = '{"invalid": json}'
        error = JSONParseError("parse failed", raw_text=raw)
        assert error.raw_text == raw
    
    def test_droid_error(self):
        """Should create Droid error"""
        error = DroidError("droid crashed")
        assert isinstance(error, ParserMakerError)
    
    def test_pipeline_error(self):
        """Should create Pipeline error"""
        error = PipelineError("pipeline failed")
        assert isinstance(error, ParserMakerError)
    
    def test_step_error(self):
        """Should create Step error"""
        error = StepError("step failed")
        assert isinstance(error, PipelineError)


class TestConfigValidation:
    """Tests for config validation"""
    
    def test_missing_glm_key_raises(self):
        """Should raise if GLM_API_KEY is missing"""
        with patch.dict('os.environ', {}, clear=True):
            with pytest.raises(Exception):
                Config()
    
    def test_model_validator(self):
        """Should load config with valid API key"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict('os.environ', {
                'GLM_API_KEY': 'test_key',
                'DROID_PROJECT_PATH': tmpdir,
            }):
                config = Config()
                assert config.glm_api_key == 'test_key'
