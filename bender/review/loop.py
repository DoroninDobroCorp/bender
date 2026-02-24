"""Review Loop v2 — простой цикл без парсинга findings.

Принцип: execute → review → передать ВЕСЬ review executor'у → git diff → repeat.
Парсер убран. Решение "продолжать/нет" принимает git status, а не LLM/regex.
"""

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, List, Optional

from ..llm_router import LLMRouter
from ..task_clarifier import TaskClarifier, ClarifiedTask
from ..worker_manager import WorkerManager, WorkerType, ManagerConfig
from ..log_filter import LogFilter
from ..log_watcher import LogWatcher

from .output_cleaner import extract_human_response, strip_prompt_header
from .prompts import REVIEW_PROMPT, REVIEW_PROMPT_PARTY, FIX_PROMPT

logger = logging.getLogger(__name__)


# ── Dataclasses ──────────────────────────────────────────

@dataclass
class ReviewLoopResult:
    """Результат review loop (backward-compatible API)."""
    success: bool
    iterations: int
    total_findings: int = 0
    fixed_findings: int = 0
    remaining_findings: list = field(default_factory=list)
    history: list = field(default_factory=list)
    cycle_detected: bool = False
    cycle_reason: str = ""
    final_message: str = ""


@dataclass
class Finding:
    """Stub для backward compatibility — больше не используется внутри."""
    severity: str
    description: str
    location: Optional[str] = None


# ── ReviewLoopManager ────────────────────────────────────

class ReviewLoopManager:
    """Итеративный цикл: execute → review → fix → git-check → repeat.

    Никакого парсинга findings — весь вывод reviewer'а передаётся
    executor'у как есть. Цикл заканчивается когда executor НЕ МЕНЯЕТ
    код (определяется через git diff).
    """

    MAX_ITERATIONS = 10

    def __init__(
        self,
        llm: LLMRouter,
        manager_config: ManagerConfig,
        on_status: Optional[Callable[[str], Awaitable[None]]] = None,
        on_question: Optional[Callable[[str], Awaitable[Optional[str]]]] = None,
        use_copilot_reviewer: bool = False,
        skip_llm: bool = False,
        use_droid_exec: bool = False,
        use_droid_review: bool = False,
        use_party_mode: bool = False,
        skip_first_execution: bool = False,
        # legacy compat (ignored)
        use_streaming: bool = False,
        use_droid_mode: bool = False,
    ):
        self.llm = llm
        self.config = manager_config
        self.on_status = on_status
        self.on_question = on_question
        self.use_party_mode = use_party_mode
        self.skip_first = skip_first_execution
        self.skip_llm = skip_llm
        self._stop_requested = False

        # Worker types
        self.exec_type = WorkerType.DROID if (use_droid_exec or use_droid_mode) else WorkerType.OPUS
        self.review_type = (
            WorkerType.DROID if use_droid_review
            else WorkerType.OPUS if use_copilot_reviewer
            else WorkerType.CODEX
        )

        # Log analysis (для report_status внутри _run_worker)
        self.log_filter = LogFilter()
        self.log_watcher = LogWatcher(llm, self.log_filter)

    # ── Properties ───────────────────────────────────

    @property
    def reviewer_type(self) -> WorkerType:
        return self.review_type

    @property
    def reviewer_name(self) -> str:
        names = {WorkerType.DROID: "droid", WorkerType.OPUS: "copilot", WorkerType.CODEX: "codex"}
        return names.get(self.review_type, "codex")

    def request_stop(self) -> None:
        self._stop_requested = True

    # ── Main loop ────────────────────────────────────

    async def run_loop(
        self,
        task: str,
        max_iterations: Optional[int] = None,
        skip_llm_analysis: bool = False,
        initial_errors: Optional[List[str]] = None,
    ) -> ReviewLoopResult:
        max_iter = max_iterations or self.MAX_ITERATIONS
        fix_count = 0

        await self._report(f"🚀 Review loop v2 (max {max_iter} iterations)")

        # 0. Уточнить задачу (опционально)
        clarified: Optional[ClarifiedTask] = None
        if not self.skip_llm and not skip_llm_analysis:
            await self._report("Analyzing task…")
            clarified = await self._clarify_task(task)

        exec_task = self._format_exec_task(task, clarified)

        # 0.5. Continue mode: передать initial_errors как review-текст
        if initial_errors:
            errors_text = "\n".join(f"- {e}" for e in initial_errors if e.strip())
            if errors_text:
                await self._report(f"Continue mode: {len(initial_errors)} initial errors")
                exec_task = FIX_PROMPT.format(task=task, review_output=errors_text)

        # 1. Первое выполнение
        if not self.skip_first:
            git_before_exec = await self._git_diff_hash()
            await self._report("Выполняю задачу…")
            exec_out, exec_ok = await self._run_worker(self.exec_type, exec_task, "exec-initial")
            if not exec_ok:
                reason = exec_out[:200] if exec_out else "unknown error"
                await self._report(f"❌ Executor сломался при первом выполнении: {reason}")
                return ReviewLoopResult(
                    success=False, iterations=0,
                    final_message=f"Worker error on initial execution: {reason}",
                )
            git_after_exec = await self._git_diff_hash()
            if git_before_exec != git_after_exec:
                fix_count += 1  # initial exec внёс изменения

        for i in range(max_iter):
            if self._stop_requested:
                await self._report("⛔ Остановлено пользователем")
                break

            iteration_num = i + 1

            # 2. Review
            review_mode = "BMAD Party" if self.use_party_mode else self.reviewer_name
            await self._report(f"=== Итерация {iteration_num}/{max_iter}: {review_mode} review ===")

            review_prompt = self._build_review_prompt(task, clarified)
            review_raw, review_ok = await self._run_worker(
                self.review_type,
                review_prompt,
                f"review-{iteration_num}",
            )
            review_text = extract_human_response(review_raw)
            review_text = strip_prompt_header(review_text)

            # Retry если review сломался или слишком короткий
            min_chars = 500 if self.use_party_mode else 200
            if not review_ok or len(review_text.strip()) < min_chars:
                reason = "worker error" if not review_ok else f"{len(review_text)} chars"
                await self._report(f"⚠️ Review failed ({reason}), retry…")
                review_raw, review_ok = await self._run_worker(
                    self.review_type,
                    review_prompt,
                    f"review-{iteration_num}-retry",
                )
                review_text = extract_human_response(review_raw)
                review_text = strip_prompt_header(review_text)

            # Если review всё ещё сломан — пропускаем итерацию
            if not review_ok or len(review_text.strip()) < 50:
                await self._report(f"❌ Reviewer не смог выполнить проверку — пропуск итерации")
                continue

            if self._stop_requested:
                break

            # 3. Git snapshot ПЕРЕД fix
            git_before = await self._git_diff_hash()

            # 4. Executor получает ВЕСЬ review
            await self._report(f"=== Итерация {iteration_num}/{max_iter}: исправление ===")
            fix_prompt = FIX_PROMPT.format(task=task, review_output=review_text)
            fix_raw, fix_ok = await self._run_worker(
                self.exec_type,
                fix_prompt,
                f"fix-{iteration_num}",
            )
            fix_text = extract_human_response(fix_raw)

            # Executor упал с ошибкой — это НЕ "нет изменений"!
            if not fix_ok:
                await self._report(f"❌ Executor сломался на итерации {iteration_num} — пропуск")
                continue

            # 5. Были ли изменения?
            git_after = await self._git_diff_hash()
            changed = git_before != git_after

            # Backup: проверяем STATUS: из вывода executor'а
            if not changed:
                changed = self._check_status_marker(fix_text)

            # ┌─────────────────────────────────────────────────────────┐
            # │ ФИЧА: "нет изменений в git = задача готова".             │
            # │ Если executor решил ничего не менять — значит review     │
            # │ не нашёл реальных проблем. НЕ ИСПРАВЛЯТЬ на "продолжать │
            # │ пока score < 98"! Это intentional design.               │
            # └─────────────────────────────────────────────────────────┘
            if not changed:
                # Извлекаем BMAD score для информативного вывода
                bmad_score = self._extract_bmad_score(review_text)
                score_label = f" (BMAD: {bmad_score}/100)" if bmad_score is not None else ""
                await self._report(f"✅ Код не изменён{score_label} — готово!")
                # Финальное сообщение: BMAD review (полный) + причина no_changes
                final_msg = self._build_final_message(review_text, fix_text)
                if final_msg:
                    logger.info(f"[ReviewLoop] Final message: {final_msg[:200]}")
                return ReviewLoopResult(
                    success=True,
                    iterations=iteration_num,
                    fixed_findings=fix_count,
                    final_message=final_msg,
                )

            fix_count += 1
            await self._report(f"📝 Код изменён (итерация {iteration_num}) — следующая проверка")

        # Max iterations
        await self._report(f"⚠️ Достигнут максимум итераций ({max_iter})")
        return ReviewLoopResult(
            success=False,
            iterations=max_iter,
            fixed_findings=fix_count,
        )

    # ── Cleanup ──────────────────────────────────────

    async def cleanup(self) -> None:
        """Очистка ресурсов (backward compat — в v2 ничего не хранится)."""
        pass

    # ── Worker execution ─────────────────────────────

    async def _run_worker(
        self,
        worker_type: WorkerType,
        task: str,
        session_suffix: str,
    ) -> tuple[str, bool]:
        """Запустить worker через WorkerManager и дождаться результата.

        Returns:
            (output, success) — текст вывода и флаг успешности.
            success=False если worker вернул ошибку, пустой вывод, или упал.
        """

        # LLM analyze callback для мониторинга
        async def llm_analyze_callback(log: str, task_text: str, elapsed: float) -> dict:
            try:
                analysis = await self.log_watcher.analyze(log, task_text, elapsed, process_alive=True)
                return {
                    "status": analysis.result.value,
                    "summary": analysis.summary,
                    "suggestion": analysis.suggestion,
                }
            except Exception as e:
                logger.debug(f"LLM analyze error: {e}")
                return {"status": "working", "summary": "Анализ недоступен"}

        worker_manager = WorkerManager(
            config=self.config,
            on_status=self.on_status,
            on_question=self.on_question,
            llm_analyze=llm_analyze_callback,
        )

        try:
            await worker_manager.start_task(
                task=task,
                worker_type=worker_type,
                context=f"bender-review-{session_suffix}",
            )

            # Периодический статус
            start_time = asyncio.get_event_loop().time()

            async def report_status():
                while True:
                    await asyncio.sleep(30)
                    elapsed = int(asyncio.get_event_loop().time() - start_time)
                    await self._report(f"⏳ {elapsed}s elapsed…")

            status_task = asyncio.create_task(report_status())

            try:
                success, output = await worker_manager.wait_for_completion(
                    timeout=self.config.stuck_timeout or 300.0,
                )
            finally:
                status_task.cancel()
                try:
                    await status_task
                except asyncio.CancelledError:
                    pass

            if not output:
                output = await worker_manager.get_output()

            output = output or ""

            # Проверяем на ошибки в выводе
            if self._is_worker_error(output):
                return output, False

            # Если worker вернул failure — определяем причину
            if not success:
                worker_status = worker_manager.get_worker_status()
                reason = self._failure_reason(worker_status)
                await self._report(f"❌ Worker {worker_type.value}: {reason}")

            return output, success

        except Exception as e:
            logger.error(f"Worker {worker_type.value} failed: {e}")
            await self._report(f"❌ Worker {worker_type.value} error: {e}")
            return "", False
        finally:
            try:
                await worker_manager.stop()
            except Exception:
                pass

    # ── Worker error detection ─────────────────────────

    _STATUS_REASONS = {
        "stuck": "вывод не менялся 5+ минут (inactivity timeout)",
        "timeout": "превышен максимальный таймаут",
        "error": "ошибка выполнения",
    }

    @classmethod
    def _failure_reason(cls, worker_status: str) -> str:
        """Человекочитаемая причина ошибки по статусу worker'а."""
        return cls._STATUS_REASONS.get(worker_status, f"статус: {worker_status}")

    # Паттерны ошибок в выводе worker'а
    _ERROR_PATTERNS = [
        "Authentication failed",
        "Payment Required",
        "402",
        "403 Forbidden",
        "429 Too Many Requests",
        "rate limit",
        "FACTORY_API_KEY",
        "ПОМИЛКА",  # Ukrainian error marker from droid
        "Error: spawn",
        "ECONNREFUSED",
        "ETIMEDOUT",
        "unauthorized",
        "access denied",
    ]

    @classmethod
    def _is_worker_error(cls, output: str) -> bool:
        """Определить, является ли вывод worker'а ошибкой, а не реальным ответом.

        Отличает ситуацию "executor решил не менять код" от
        "executor сломался и не смог работать".
        """
        if not output or len(output.strip()) < 50:
            return True

        # Длинный вывод (>500 символов) — это реальная работа, не ошибка.
        # Паттерны типа "402" могут ложно матчиться в большом тексте.
        if len(output.strip()) > 500:
            return False

        output_lower = output.lower()
        for pattern in cls._ERROR_PATTERNS:
            if pattern.lower() in output_lower:
                return True

        return False

    # ── Git change detection ─────────────────────────

    async def _git_diff_hash(self) -> str:
        """MD5-hash текущего git diff — для сравнения до/после."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "diff", "HEAD", "--stat",
                cwd=str(self.config.project_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            # Добавляем untracked files
            proc2 = await asyncio.create_subprocess_exec(
                "git", "status", "--porcelain",
                cwd=str(self.config.project_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout2, _ = await proc2.communicate()
            combined = stdout + stdout2
            return hashlib.md5(combined).hexdigest()
        except Exception as e:
            logger.warning(f"git diff hash failed: {e}")
            return ""

    # ── Status marker parsing ────────────────────────

    @staticmethod
    def _check_status_marker(text: str) -> bool:
        """Проверить STATUS: CHANGED / NO_CHANGES в выводе executor'а."""
        if not text:
            return False
        # Ищем последнее вхождение STATUS:
        lines = text.strip().split('\n')
        for line in reversed(lines):
            stripped = line.strip().upper()
            if 'STATUS:' in stripped:
                if 'CHANGED' in stripped and 'NO_CHANGES' not in stripped:
                    return True
                if 'NO_CHANGES' in stripped:
                    return False
        return False

    @staticmethod
    def _extract_no_changes_reason(text: str) -> str:
        """Извлечь объяснение почему executor НЕ менял код."""
        if not text:
            return ""
        upper = text.upper()
        idx = upper.rfind("STATUS: NO_CHANGES")
        if idx == -1:
            idx = upper.rfind("STATUS:")
        if idx == -1:
            # Вернём последние 500 символов как fallback
            return text[-500:].strip()
        # Берём текст после STATUS:
        after = text[idx:].split('\n', 1)
        if len(after) > 1:
            return after[1].strip()[:500]
        return text[:idx].strip()[-500:]

    @staticmethod
    def _build_final_message(review_text: str, fix_text: str) -> str:
        """Собрать полный финальный ответ: BMAD review + причина no_changes.

        Не обрезаем — safe_send_message в bender.py сам разобьёт на чанки.
        """
        parts = []
        if review_text and len(review_text.strip()) > 100:
            parts.append(review_text.strip())
        if fix_text:
            reason = ReviewLoopManager._extract_no_changes_reason(fix_text)
            if reason:
                parts.append(f"--- Executor ---\n{reason}")
        return "\n\n".join(parts) if parts else ""

    @staticmethod
    def _extract_bmad_score(review_text: str) -> Optional[int]:
        """Извлечь Average score из BMAD Party review.

        Ищет паттерн 'Average: XX/100' или 'Average = XX/100' в тексте review.
        Возвращает None если это не party review или score не найден.
        """
        if not review_text:
            return None
        match = re.search(r'Average[:\s=]+(\d+(?:\.\d+)?)\s*/\s*100', review_text)
        if match:
            return int(float(match.group(1)))
        return None

    # ── Task formatting ──────────────────────────────

    def _format_exec_task(self, task: str, clarified: Optional[ClarifiedTask]) -> str:
        if clarified and clarified.acceptance_criteria:
            criteria = "\n".join(f"  {i+1}. {c}" for i, c in enumerate(clarified.acceptance_criteria))
            return f"""{clarified.clarified_task}

📝 Acceptance Criteria:
{criteria}

Выполни ВСЕ пункты. После завершения проверь что каждый критерий выполнен."""
        return task

    def _build_review_prompt(self, task: str, clarified: Optional[ClarifiedTask]) -> str:
        template = REVIEW_PROMPT_PARTY if self.use_party_mode else REVIEW_PROMPT
        criteria = "Нет явных критериев"
        if clarified and clarified.acceptance_criteria:
            criteria = "\n".join(f"- {c}" for c in clarified.acceptance_criteria)
        return template.format(context=task, criteria=criteria)

    # ── Task clarification ───────────────────────────

    async def _clarify_task(self, task: str) -> Optional[ClarifiedTask]:
        try:
            clarifier = TaskClarifier(
                llm=self.llm,
                project_path=self.config.project_path,
                on_ask_user=self.on_question,
            )
            return await clarifier.clarify(task)
        except Exception as e:
            logger.warning(f"Task clarification failed: {e}")
            return None

    # ── Status reporting ─────────────────────────────

    async def _report(self, message: str) -> None:
        logger.info(f"[ReviewLoop] {message}")
        if self.on_status:
            await self.on_status(f"[Loop] {message}")
