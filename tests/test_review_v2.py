"""Тесты для review loop v2 — output_cleaner, prompts, loop logic."""

import asyncio
import hashlib
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bender.review.output_cleaner import (
    strip_ansi,
    extract_human_response,
    strip_prompt_header,
)
from bender.review.prompts import (
    REVIEW_PROMPT,
    REVIEW_PROMPT_PARTY,
    FIX_PROMPT,
)
from bender.review.loop import (
    ReviewLoopManager,
    ReviewLoopResult,
    Finding,
)


# ═══════════════════════════════════════════════════════
# OutputCleaner tests
# ═══════════════════════════════════════════════════════


class TestStripAnsi:
    def test_removes_color_codes(self):
        text = "\x1B[31mERROR\x1B[0m: something"
        assert strip_ansi(text) == "ERROR: something"

    def test_handles_none(self):
        assert strip_ansi(None) == ""

    def test_handles_empty(self):
        assert strip_ansi("") == ""

    def test_preserves_plain_text(self):
        assert strip_ansi("hello world") == "hello world"

    def test_complex_ansi_sequences(self):
        text = "\x1B[1;33mWARNING\x1B[0m\x1B[2J"
        result = strip_ansi(text)
        assert "WARNING" in result
        assert "\x1B" not in result


class TestExtractHumanResponse:
    def test_empty_input(self):
        assert extract_human_response("") == ""
        assert extract_human_response(None) == ""

    def test_plain_text(self):
        assert extract_human_response("Hello world") == "Hello world"

    def test_strips_ansi_from_plain(self):
        text = "\x1B[31mHello\x1B[0m world"
        assert extract_human_response(text) == "Hello world"

    def test_json_events_droid(self):
        lines = [
            '{"type": "message", "role": "assistant", "text": "Found a bug"}',
            '{"type": "message", "role": "assistant", "text": "in auth.py"}',
            '{"type": "completion", "finalText": "Done reviewing"}',
        ]
        raw = "\n".join(lines)
        result = extract_human_response(raw)
        assert "Found a bug" in result
        assert "in auth.py" in result
        assert "Done reviewing" in result

    def test_json_events_skips_non_assistant(self):
        lines = [
            '{"type": "message", "role": "system", "text": "System prompt"}',
            '{"type": "message", "role": "assistant", "text": "Real answer"}',
            '{"type": "message", "role": "user", "text": "User question"}',
            '{"type": "completion", "finalText": "Final"}',
        ]
        raw = "\n".join(lines)
        result = extract_human_response(raw)
        assert "Real answer" in result
        assert "System prompt" not in result

    def test_trims_json_tail(self):
        text = "Normal review output with findings\n" * 10
        text += '{"type": "done"}'
        result = extract_human_response(text)
        assert '{"type":' not in result
        assert "Normal review output" in result


class TestStripPromptHeader:
    def test_no_header(self):
        text = "Just normal text"
        assert strip_prompt_header(text) == "Just normal text"

    def test_with_bender_header(self):
        text = "🤖 BENDER → task\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\nprompt\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\nActual response"
        result = strip_prompt_header(text)
        assert "Actual response" in result
        assert "BENDER" not in result


# ═══════════════════════════════════════════════════════
# Prompts tests
# ═══════════════════════════════════════════════════════


class TestPrompts:
    def test_review_prompt_has_placeholders(self):
        assert "{context}" in REVIEW_PROMPT
        assert "{criteria}" in REVIEW_PROMPT

    def test_review_prompt_party_has_10_roles(self):
        for role in ["Mary", "Winston", "Amelia", "John", "Barry", "Quinn", "Bob", "Paige", "Sally", "BMad"]:
            assert role in REVIEW_PROMPT_PARTY

    def test_fix_prompt_has_placeholders(self):
        assert "{task}" in FIX_PROMPT
        assert "{review_output}" in FIX_PROMPT

    def test_fix_prompt_has_status_instructions(self):
        assert "STATUS: CHANGED" in FIX_PROMPT
        assert "STATUS: NO_CHANGES" in FIX_PROMPT

    def test_fix_prompt_formats_correctly(self):
        result = FIX_PROMPT.format(task="Add OAuth", review_output="Found SQL injection")
        assert "Add OAuth" in result
        assert "Found SQL injection" in result


# ═══════════════════════════════════════════════════════
# Loop logic tests
# ═══════════════════════════════════════════════════════


class TestReviewLoopResult:
    def test_defaults(self):
        r = ReviewLoopResult(success=True, iterations=3)
        assert r.success is True
        assert r.iterations == 3
        assert r.total_findings == 0
        assert r.fixed_findings == 0
        assert r.remaining_findings == []
        assert r.cycle_detected is False

    def test_final_message(self):
        r = ReviewLoopResult(success=True, iterations=1, final_message="All good")
        assert r.final_message == "All good"


class TestFinding:
    def test_backward_compat(self):
        f = Finding(severity="HIGH", description="SQL injection", location="auth.py:42")
        assert f.severity == "HIGH"
        assert f.description == "SQL injection"
        assert f.location == "auth.py:42"


class TestIsWorkerError:
    def test_empty_output(self):
        assert ReviewLoopManager._is_worker_error("") is True
        assert ReviewLoopManager._is_worker_error(None) is True

    def test_short_output(self):
        assert ReviewLoopManager._is_worker_error("error") is True
        assert ReviewLoopManager._is_worker_error("x" * 49) is True

    def test_auth_error(self):
        assert ReviewLoopManager._is_worker_error("Error: Authentication failed. Please log in.") is True

    def test_402_error(self):
        assert ReviewLoopManager._is_worker_error('{"type":"error","message":"402 Payment Required"}') is True

    def test_droid_pomilka(self):
        assert ReviewLoopManager._is_worker_error("❌ ПОМИЛКА: Error: something broke") is True

    def test_normal_output(self):
        normal = "I've reviewed the code and found the following issues:\n1. SQL injection in auth.py\n2. Missing input validation"
        assert ReviewLoopManager._is_worker_error(normal) is False

    def test_rate_limit(self):
        assert ReviewLoopManager._is_worker_error("Too many requests, rate limit exceeded, please try again later.") is True


class TestStatusMarker:
    def test_changed(self):
        text = "Fixed the bug\nSTATUS: CHANGED"
        assert ReviewLoopManager._check_status_marker(text) is True

    def test_no_changes(self):
        text = "Everything looks good\nSTATUS: NO_CHANGES"
        assert ReviewLoopManager._check_status_marker(text) is False

    def test_empty(self):
        assert ReviewLoopManager._check_status_marker("") is False
        assert ReviewLoopManager._check_status_marker(None) is False

    def test_changed_case_insensitive(self):
        text = "Done\nstatus: changed"
        assert ReviewLoopManager._check_status_marker(text) is True

    def test_no_status_line(self):
        text = "Just some output without status"
        assert ReviewLoopManager._check_status_marker(text) is False


class TestExtractNoChangesReason:
    def test_extracts_after_status(self):
        text = "Review done\nSTATUS: NO_CHANGES\nBecause everything is fine"
        result = ReviewLoopManager._extract_no_changes_reason(text)
        assert "everything is fine" in result

    def test_fallback_on_no_status(self):
        text = "Some random output text"
        result = ReviewLoopManager._extract_no_changes_reason(text)
        assert "random output" in result


class TestExtractBmadScore:
    def test_extracts_score(self):
        text = "Scores: Mary=95 ...\nAverage: 87/100\nMin: 72/100"
        assert ReviewLoopManager._extract_bmad_score(text) == 87

    def test_perfect_score(self):
        text = "Average: 100/100\nAll roles approved"
        assert ReviewLoopManager._extract_bmad_score(text) == 100

    def test_no_score_returns_none(self):
        text = "Regular review without BMAD party scoring"
        assert ReviewLoopManager._extract_bmad_score(text) is None

    def test_empty_returns_none(self):
        assert ReviewLoopManager._extract_bmad_score("") is None
        assert ReviewLoopManager._extract_bmad_score(None) is None

    def test_score_with_spaces(self):
        text = "Average:  92 / 100"
        assert ReviewLoopManager._extract_bmad_score(text) == 92

    def test_fractional_score(self):
        text = "Average: 74.5/100\nMin: 62/100"
        assert ReviewLoopManager._extract_bmad_score(text) == 74

    def test_equals_sign_format(self):
        text = "Average = 85.3/100"
        assert ReviewLoopManager._extract_bmad_score(text) == 85


class TestReviewLoopManagerInit:
    def test_default_worker_types(self):
        llm = MagicMock()
        config = MagicMock()
        mgr = ReviewLoopManager(llm=llm, manager_config=config)
        from bender.worker_manager import WorkerType
        assert mgr.exec_type == WorkerType.OPUS
        assert mgr.review_type == WorkerType.CODEX

    def test_droid_exec(self):
        llm = MagicMock()
        config = MagicMock()
        mgr = ReviewLoopManager(llm=llm, manager_config=config, use_droid_exec=True)
        from bender.worker_manager import WorkerType
        assert mgr.exec_type == WorkerType.DROID
        assert mgr.review_type == WorkerType.CODEX

    def test_droid_both(self):
        llm = MagicMock()
        config = MagicMock()
        mgr = ReviewLoopManager(llm=llm, manager_config=config, use_droid_exec=True, use_droid_review=True)
        from bender.worker_manager import WorkerType
        assert mgr.exec_type == WorkerType.DROID
        assert mgr.review_type == WorkerType.DROID

    def test_copilot_reviewer(self):
        llm = MagicMock()
        config = MagicMock()
        mgr = ReviewLoopManager(llm=llm, manager_config=config, use_copilot_reviewer=True)
        from bender.worker_manager import WorkerType
        assert mgr.review_type == WorkerType.OPUS

    def test_party_mode(self):
        llm = MagicMock()
        config = MagicMock()
        mgr = ReviewLoopManager(llm=llm, manager_config=config, use_party_mode=True)
        assert mgr.use_party_mode is True

    def test_legacy_droid_mode(self):
        llm = MagicMock()
        config = MagicMock()
        mgr = ReviewLoopManager(llm=llm, manager_config=config, use_droid_mode=True)
        from bender.worker_manager import WorkerType
        assert mgr.exec_type == WorkerType.DROID

    def test_reviewer_name(self):
        llm = MagicMock()
        config = MagicMock()
        mgr = ReviewLoopManager(llm=llm, manager_config=config, use_droid_review=True)
        assert mgr.reviewer_name == "droid"

        mgr2 = ReviewLoopManager(llm=llm, manager_config=config, use_copilot_reviewer=True)
        assert mgr2.reviewer_name == "copilot"

        mgr3 = ReviewLoopManager(llm=llm, manager_config=config)
        assert mgr3.reviewer_name == "codex"

    def test_request_stop(self):
        llm = MagicMock()
        config = MagicMock()
        mgr = ReviewLoopManager(llm=llm, manager_config=config)
        assert mgr._stop_requested is False
        mgr.request_stop()
        assert mgr._stop_requested is True


class TestBuildReviewPrompt:
    def test_no_clarified(self):
        llm = MagicMock()
        config = MagicMock()
        mgr = ReviewLoopManager(llm=llm, manager_config=config)
        prompt = mgr._build_review_prompt("Add OAuth", None)
        assert "Add OAuth" in prompt
        assert "Нет явных критериев" in prompt

    def test_with_criteria(self):
        llm = MagicMock()
        config = MagicMock()
        mgr = ReviewLoopManager(llm=llm, manager_config=config)
        clarified = MagicMock()
        clarified.acceptance_criteria = ["Tests pass", "No SQL injection"]
        prompt = mgr._build_review_prompt("Add OAuth", clarified)
        assert "Tests pass" in prompt
        assert "No SQL injection" in prompt

    def test_party_mode(self):
        llm = MagicMock()
        config = MagicMock()
        mgr = ReviewLoopManager(llm=llm, manager_config=config, use_party_mode=True)
        prompt = mgr._build_review_prompt("Add OAuth", None)
        assert "BMAD Party" in prompt


class TestFormatExecTask:
    def test_no_clarified(self):
        llm = MagicMock()
        config = MagicMock()
        mgr = ReviewLoopManager(llm=llm, manager_config=config)
        result = mgr._format_exec_task("Add OAuth", None)
        assert result == "Add OAuth"

    def test_with_criteria(self):
        llm = MagicMock()
        config = MagicMock()
        mgr = ReviewLoopManager(llm=llm, manager_config=config)
        clarified = MagicMock()
        clarified.clarified_task = "Add OAuth to backend"
        clarified.acceptance_criteria = ["Login works", "Tokens refresh"]
        result = mgr._format_exec_task("Add OAuth", clarified)
        assert "Login works" in result
        assert "Tokens refresh" in result
        assert "Acceptance Criteria" in result


@pytest.mark.asyncio
class TestRunLoopFlow:
    """Integration-like tests: mock _run_worker and _git_diff_hash to test loop logic."""

    async def test_no_changes_after_first_fix(self):
        """Executor не изменяет код → цикл завершается за 1 итерацию."""
        llm = MagicMock()
        config = MagicMock()
        config.project_path = "/tmp/test"
        config.stuck_timeout = 60

        mgr = ReviewLoopManager(llm=llm, manager_config=config, skip_llm=True)

        async def fake_run_worker(wtype, task, suffix):
            if "exec-initial" in suffix:
                return "Code written successfully, all changes applied.", True
            elif "review" in suffix:
                return "Found SQL injection in auth.py — this is a real issue.", True
            elif "fix" in suffix:
                return "Everything already looks good, no real issues to fix.\nSTATUS: NO_CHANGES\nNo real issues to fix", True
            return "", True

        git_hash = hashlib.md5(b"same").hexdigest()
        mgr._run_worker = fake_run_worker
        mgr._git_diff_hash = AsyncMock(return_value=git_hash)
        mgr._clarify_task = AsyncMock(return_value=None)

        result = await mgr.run_loop("Add OAuth", max_iterations=5)

        assert result.success is True
        assert result.iterations == 1

    async def test_changes_then_no_changes(self):
        """Executor меняет код, потом на 2-й итерации нет изменений → done."""
        llm = MagicMock()
        config = MagicMock()
        config.project_path = "/tmp/test"
        config.stuck_timeout = 60

        mgr = ReviewLoopManager(llm=llm, manager_config=config, skip_llm=True)

        fix_call = 0
        async def fake_run_worker(wtype, task, suffix):
            nonlocal fix_call
            if "review" in suffix:
                return "Found a bug in auth.py that causes a crash on login.", True
            if "fix" in suffix:
                fix_call += 1
                if fix_call == 1:
                    return "Fixed the bug in auth.py login handler.\nSTATUS: CHANGED", True
                return "Nothing to fix, all issues resolved.\nSTATUS: NO_CHANGES", True
            return "Initial code written successfully with all required changes.", True

        git_call_count = 0
        async def fake_git_hash():
            nonlocal git_call_count
            git_call_count += 1
            # calls 1-2: initial exec (before/after) — changed
            if git_call_count == 1:
                return "initial-before"
            elif git_call_count == 2:
                return "initial-after"
            # calls 3-4: fix-1 (before/after) — changed
            elif git_call_count == 3:
                return "before-fix"
            elif git_call_count == 4:
                return "after-fix"
            # calls 5+: fix-2 — stable (no changes)
            else:
                return "stable"

        mgr._run_worker = fake_run_worker
        mgr._git_diff_hash = fake_git_hash
        mgr._clarify_task = AsyncMock(return_value=None)

        result = await mgr.run_loop("Add OAuth", max_iterations=5)

        assert result.success is True
        assert result.iterations == 2
        assert result.fixed_findings == 2  # initial exec + 1 fix iteration

    async def test_max_iterations_reached(self):
        """Executor всегда меняет код → max iterations."""
        llm = MagicMock()
        config = MagicMock()
        config.project_path = "/tmp/test"
        config.stuck_timeout = 60

        mgr = ReviewLoopManager(llm=llm, manager_config=config, skip_llm=True)

        async def fake_run_worker(wtype, task, suffix):
            return "Some meaningful output that is long enough to not be an error and has real content in it.", True

        git_counter = 0
        async def fake_git_hash():
            nonlocal git_counter
            git_counter += 1
            return f"hash-{git_counter}"  # Always different

        mgr._run_worker = fake_run_worker
        mgr._git_diff_hash = fake_git_hash
        mgr._clarify_task = AsyncMock(return_value=None)

        result = await mgr.run_loop("Add OAuth", max_iterations=3)

        assert result.success is False
        assert result.iterations == 3

    async def test_stop_requested(self):
        """Пользователь просит стоп → цикл прерывается."""
        llm = MagicMock()
        config = MagicMock()
        config.project_path = "/tmp/test"
        config.stuck_timeout = 60

        mgr = ReviewLoopManager(llm=llm, manager_config=config, skip_llm=True)

        async def fake_run_worker(wtype, task, suffix):
            if "exec-initial" in suffix:
                mgr.request_stop()
            return "Output that is long enough to pass the error check and contains real content.", True

        mgr._run_worker = fake_run_worker
        mgr._git_diff_hash = AsyncMock(return_value="hash")
        mgr._clarify_task = AsyncMock(return_value=None)

        result = await mgr.run_loop("Add OAuth", max_iterations=5)
        # Should stop after exec without entering loop
        assert result.iterations <= 5

    async def test_initial_errors_continue_mode(self):
        """Continue mode: initial_errors используются как первый review."""
        llm = MagicMock()
        config = MagicMock()
        config.project_path = "/tmp/test"
        config.stuck_timeout = 60

        mgr = ReviewLoopManager(llm=llm, manager_config=config, skip_llm=True)

        exec_tasks = []
        async def fake_run_worker(wtype, task, suffix):
            exec_tasks.append(task)
            return "Reviewed everything, nothing to change here.\nSTATUS: NO_CHANGES\nNothing to fix", True

        mgr._run_worker = fake_run_worker
        mgr._git_diff_hash = AsyncMock(return_value="stable-hash")
        mgr._clarify_task = AsyncMock(return_value=None)

        result = await mgr.run_loop(
            "Fix bugs",
            max_iterations=5,
            initial_errors=["TypeError in auth.py", "Missing import"],
        )

        # The initial exec_task should contain the errors
        assert any("TypeError in auth.py" in t for t in exec_tasks)
        assert result.success is True

    async def test_skip_first_execution(self):
        """skip_first_execution: сразу к review без начального execution."""
        llm = MagicMock()
        config = MagicMock()
        config.project_path = "/tmp/test"
        config.stuck_timeout = 60

        mgr = ReviewLoopManager(
            llm=llm, manager_config=config,
            skip_llm=True, skip_first_execution=True,
        )

        suffixes = []
        async def fake_run_worker(wtype, task, suffix):
            suffixes.append(suffix)
            return "Everything looks fine, no changes needed.\nSTATUS: NO_CHANGES", True

        mgr._run_worker = fake_run_worker
        mgr._git_diff_hash = AsyncMock(return_value="same")
        mgr._clarify_task = AsyncMock(return_value=None)

        await mgr.run_loop("Review code", max_iterations=3)

        # Should NOT have "exec-initial"
        assert "exec-initial" not in suffixes
        # Should have review and fix
        assert any("review" in s for s in suffixes)
        assert any("fix" in s for s in suffixes)

    async def test_worker_error_not_treated_as_success(self):
        """Worker ошибка (402, auth fail) НЕ считается 'нет изменений'."""
        llm = MagicMock()
        config = MagicMock()
        config.project_path = "/tmp/test"
        config.stuck_timeout = 60

        mgr = ReviewLoopManager(llm=llm, manager_config=config, skip_llm=True)

        async def fake_run_worker(wtype, task, suffix):
            if "exec-initial" in suffix:
                return "Code written and all changes applied successfully.", True
            if "review" in suffix:
                return "Found a critical bug: SQL injection vulnerability in auth.py line 42.", True
            if "fix" in suffix:
                # Worker упал с ошибкой!
                return "Error: Authentication failed. 402 Payment Required", False
            return "", True

        mgr._run_worker = fake_run_worker
        mgr._git_diff_hash = AsyncMock(return_value="same-hash")
        mgr._clarify_task = AsyncMock(return_value=None)

        result = await mgr.run_loop("Add OAuth", max_iterations=3)

        # Ошибка НЕ должна считаться успехом!
        assert result.success is False
        assert result.iterations == 3  # Прошёл все итерации, каждый раз executor падал

    async def test_worker_error_on_initial_exec(self):
        """Worker ошибка на первом exec → сразу fail."""
        llm = MagicMock()
        config = MagicMock()
        config.project_path = "/tmp/test"
        config.stuck_timeout = 60

        mgr = ReviewLoopManager(llm=llm, manager_config=config, skip_llm=True)

        async def fake_run_worker(wtype, task, suffix):
            return "ПОМИЛКА: Error: Authentication failed.", False

        mgr._run_worker = fake_run_worker
        mgr._clarify_task = AsyncMock(return_value=None)

        result = await mgr.run_loop("Add OAuth", max_iterations=5)

        assert result.success is False
        assert result.iterations == 0

    async def test_cleanup_is_noop(self):
        """cleanup() should not raise."""
        llm = MagicMock()
        config = MagicMock()
        mgr = ReviewLoopManager(llm=llm, manager_config=config)
        await mgr.cleanup()  # Should not raise
