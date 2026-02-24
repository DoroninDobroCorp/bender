"""Tests for _extract_findings_with_llm() — AC-4, AC-5.

LLM-based extraction of findings from reviewer output.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bender.review_loop_legacy import Finding, ReviewLoopManager
from bender.worker_manager import ManagerConfig


def make_manager() -> ReviewLoopManager:
    """Create ReviewLoopManager with mocked LLM and minimal config."""
    mock_llm = MagicMock()
    mock_llm.generate_json = AsyncMock()
    mock_llm.generate = AsyncMock(return_value="")
    config = ManagerConfig(project_path=Path("/tmp/test_project"))
    manager = ReviewLoopManager(llm=mock_llm, manager_config=config)
    return manager


class TestExtractFindingsWithLlm:
    """Тесты для _extract_findings_with_llm()."""

    @pytest.mark.asyncio
    async def test_success_extracts_findings(self) -> None:
        """LLM возвращает findings — они корректно парсятся."""
        manager = make_manager()
        manager.llm.generate_json.return_value = {
            "has_issues": True,
            "summary": "Found security issue",
            "findings": [
                {"severity": "HIGH", "description": "SQL injection risk", "location": "api.py:42"},
                {"severity": "MEDIUM", "description": "Missing validation", "location": None},
            ],
        }

        findings = await manager._extract_findings_with_llm("review output here")

        assert len(findings) == 2
        assert findings[0].severity == "HIGH"
        assert findings[0].description == "SQL injection risk"
        assert findings[0].location == "api.py:42"
        assert findings[1].severity == "MEDIUM"
        assert findings[1].location is None

    @pytest.mark.asyncio
    async def test_no_issues_returns_empty(self) -> None:
        """Если LLM говорит has_issues=False — возвращаем пустой список."""
        manager = make_manager()
        manager.llm.generate_json.return_value = {
            "has_issues": False,
            "summary": "All good",
            "findings": [],
        }

        findings = await manager._extract_findings_with_llm("looks fine")

        assert findings == []

    @pytest.mark.asyncio
    async def test_fallback_on_llm_error(self) -> None:
        """При ошибке LLM — fallback на _parse_findings()."""
        manager = make_manager()
        manager.llm.generate_json.side_effect = Exception("LLM timeout")

        # _parse_findings() is the fallback - mock it
        with patch.object(manager, "_parse_findings", return_value=[]) as mock_parse:
            findings = await manager._extract_findings_with_llm("some output")
            mock_parse.assert_called_once()
            assert findings == []

    @pytest.mark.asyncio
    async def test_empty_description_filtered_out(self) -> None:
        """Finding с пустым description игнорируется."""
        manager = make_manager()
        manager.llm.generate_json.return_value = {
            "has_issues": True,
            "summary": "Issues found",
            "findings": [
                {"severity": "HIGH", "description": "", "location": "file.py:1"},
                {"severity": "MEDIUM", "description": "Real issue", "location": None},
            ],
        }

        findings = await manager._extract_findings_with_llm("review output")

        assert len(findings) == 1
        assert findings[0].description == "Real issue"

    @pytest.mark.asyncio
    async def test_severity_mapping_uppercase(self) -> None:
        """Severity корректно нормализуется в uppercase."""
        manager = make_manager()
        manager.llm.generate_json.return_value = {
            "has_issues": True,
            "summary": "Issues found",
            "findings": [
                {"severity": "critical", "description": "Critical issue", "location": None},
                {"severity": "LOW", "description": "Low issue", "location": None},
            ],
        }

        findings = await manager._extract_findings_with_llm("review output")

        assert findings[0].severity == "CRITICAL"
        assert findings[1].severity == "LOW"

    @pytest.mark.asyncio
    async def test_null_location_normalized(self) -> None:
        """location='null' строкой нормализуется в None."""
        manager = make_manager()
        manager.llm.generate_json.return_value = {
            "has_issues": True,
            "summary": "Found issue",
            "findings": [
                {"severity": "MEDIUM", "description": "Issue", "location": "null"},
                {"severity": "LOW", "description": "Minor", "location": ""},
            ],
        }

        findings = await manager._extract_findings_with_llm("output")

        assert findings[0].location is None
        assert findings[1].location is None

    @pytest.mark.asyncio
    async def test_truncation_for_long_output(self) -> None:
        """Длинный вывод обрезается до 15000 символов."""
        manager = make_manager()
        manager.llm.generate_json.return_value = {
            "has_issues": False,
            "summary": "No issues",
            "findings": [],
        }

        long_output = "x" * 20000
        await manager._extract_findings_with_llm(long_output)

        # Проверяем что prompt был вызван (generate_json was called)
        manager.llm.generate_json.assert_called_once()
        # Prompt должен содержать truncated output (не полные 20k символов)
        call_args = manager.llm.generate_json.call_args
        prompt = call_args[0][0] if call_args[0] else call_args.kwargs.get("prompt", "")
        assert "truncated" in prompt


class TestShouldContinuePrompt:
    """Проверяем наличие и содержание SHOULD_CONTINUE_PROMPT константы."""

    def test_constant_exists(self) -> None:
        """SHOULD_CONTINUE_PROMPT должна быть определена."""
        from bender.review_loop_legacy import SHOULD_CONTINUE_PROMPT
        assert isinstance(SHOULD_CONTINUE_PROMPT, str)
        assert len(SHOULD_CONTINUE_PROMPT) > 0

    def test_contains_severity_rules(self) -> None:
        """Промпт содержит правила по severity."""
        from bender.review_loop_legacy import SHOULD_CONTINUE_PROMPT
        assert "LOW" in SHOULD_CONTINUE_PROMPT
        assert "MEDIUM" in SHOULD_CONTINUE_PROMPT
        assert "CRITICAL" in SHOULD_CONTINUE_PROMPT

    def test_json_format_required(self) -> None:
        """Промпт требует JSON ответ."""
        from bender.review_loop_legacy import SHOULD_CONTINUE_PROMPT
        assert "continue" in SHOULD_CONTINUE_PROMPT
        assert "JSON" in SHOULD_CONTINUE_PROMPT


class TestExtractFindingsPrompt:
    """Проверяем наличие и содержание EXTRACT_FINDINGS_PROMPT константы."""

    def test_constant_exists(self) -> None:
        """EXTRACT_FINDINGS_PROMPT должна быть определена."""
        from bender.review_loop_legacy import EXTRACT_FINDINGS_PROMPT
        assert isinstance(EXTRACT_FINDINGS_PROMPT, str)
        assert len(EXTRACT_FINDINGS_PROMPT) > 0

    def test_contains_expected_json_structure(self) -> None:
        """Промпт описывает ожидаемую JSON структуру."""
        from bender.review_loop_legacy import EXTRACT_FINDINGS_PROMPT
        assert "has_issues" in EXTRACT_FINDINGS_PROMPT
        assert "findings" in EXTRACT_FINDINGS_PROMPT
        assert "severity" in EXTRACT_FINDINGS_PROMPT


class TestPartyMode:
    """Тесты для BMAD Party mode (-P)."""

    def test_party_prompt_constant_exists(self) -> None:
        """REVIEW_TASK_PARTY должна быть определена."""
        from bender.review_loop_legacy import REVIEW_TASK_PARTY
        assert isinstance(REVIEW_TASK_PARTY, str)
        assert len(REVIEW_TASK_PARTY) > 0

    def test_party_prompt_has_10_roles(self) -> None:
        """REVIEW_TASK_PARTY содержит все 10 ролей."""
        from bender.review_loop_legacy import REVIEW_TASK_PARTY
        roles = ["Mary", "Winston", "Amelia", "John", "Barry",
                 "Quinn", "Bob", "Paige", "Sally", "BMad"]
        for role in roles:
            assert role in REVIEW_TASK_PARTY, f"Role {role} missing"

    def test_party_prompt_has_real_tool_checks(self) -> None:
        """REVIEW_TASK_PARTY требует реальные проверки pytest/mypy/ruff."""
        from bender.review_loop_legacy import REVIEW_TASK_PARTY
        assert "pytest" in REVIEW_TASK_PARTY
        assert "mypy" in REVIEW_TASK_PARTY
        assert "ruff" in REVIEW_TASK_PARTY

    def test_party_prompt_format_placeholders(self) -> None:
        """REVIEW_TASK_PARTY форматируется с {context} и {criteria}."""
        from bender.review_loop_legacy import REVIEW_TASK_PARTY
        result = REVIEW_TASK_PARTY.format(context="test task", criteria="AC-1")
        assert "test task" in result
        assert "AC-1" in result

    def test_party_mode_flag_default_false(self) -> None:
        """use_party_mode по умолчанию False."""
        manager = make_manager()
        assert manager.use_party_mode is False

    def test_party_mode_flag_set_true(self) -> None:
        """use_party_mode устанавливается при передаче True."""
        mock_llm = MagicMock()
        mock_llm.generate_json = AsyncMock()
        mock_llm.generate = AsyncMock(return_value="")
        config = ManagerConfig(project_path=Path("/tmp/test_project"))
        manager = ReviewLoopManager(
            llm=mock_llm, manager_config=config, use_party_mode=True,
        )
        assert manager.use_party_mode is True

    def test_party_mode_compatible_with_droid(self) -> None:
        """Party mode совместим с droid execution."""
        mock_llm = MagicMock()
        mock_llm.generate_json = AsyncMock()
        mock_llm.generate = AsyncMock(return_value="")
        config = ManagerConfig(project_path=Path("/tmp/test_project"))
        manager = ReviewLoopManager(
            llm=mock_llm, manager_config=config,
            use_party_mode=True, use_droid_exec=True,
        )
        assert manager.use_party_mode is True
        assert manager.use_droid_exec is True

    def test_party_mode_compatible_with_copilot_reviewer(self) -> None:
        """Party mode совместим с copilot reviewer."""
        mock_llm = MagicMock()
        mock_llm.generate_json = AsyncMock()
        mock_llm.generate = AsyncMock(return_value="")
        config = ManagerConfig(project_path=Path("/tmp/test_project"))
        manager = ReviewLoopManager(
            llm=mock_llm, manager_config=config,
            use_party_mode=True, use_copilot_reviewer=True,
        )
        assert manager.use_party_mode is True
        assert manager.use_copilot_reviewer is True
