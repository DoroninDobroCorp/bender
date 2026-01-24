"""
Task Manager - управление задачами с умным выбором worker'а

Новый flow:
1. Уточнение ТЗ (TaskClarifier)
2. Автовыбор worker'а по сложности
3. Мониторинг с nudge вместо restart
4. Финальный review если много изменений
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional, List, Callable, Awaitable
from enum import Enum
from datetime import datetime

from .worker_manager import WorkerManager, WorkerType, ManagerConfig
from .log_watcher import LogWatcher, AnalysisResult, WatcherAnalysis
from .log_filter import LogFilter
from .glm_client import GLMClient
from .task_clarifier import TaskClarifier, TaskComplexity, ClarifiedTask

logger = logging.getLogger(__name__)


class TaskState(str, Enum):
    """Состояние задачи"""
    CLARIFYING = "clarifying"
    PENDING = "pending"
    RUNNING = "running"
    NUDGING = "nudging"
    VERIFYING = "verifying"
    REVIEWING = "reviewing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class TaskResult:
    """Результат выполнения задачи"""
    task: str
    state: TaskState
    worker_type: WorkerType
    attempts: int = 1
    nudges: int = 0
    total_time: float = 0.0
    verification_passed: bool = False
    final_summary: str = ""
    error: Optional[str] = None
    # Token usage (только для copilot worker)
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    # Clarification info
    complexity: Optional[TaskComplexity] = None
    acceptance_criteria: List[str] = field(default_factory=list)


@dataclass 
class TaskHistory:
    """История попытки выполнения"""
    attempt: int
    worker_type: WorkerType
    duration: float
    analysis: WatcherAnalysis
    timestamp: datetime = field(default_factory=datetime.now)


# Маппинг сложности на worker
COMPLEXITY_TO_WORKER = {
    TaskComplexity.SIMPLE: WorkerType.DROID,
    TaskComplexity.MEDIUM: WorkerType.OPUS,
    TaskComplexity.COMPLEX: WorkerType.CODEX,
}


class TaskManager:
    """Менеджер задач с умным flow
    
    Flow:
    1. Уточнение ТЗ → чёткие критерии + сложность
    2. Автовыбор worker'а (droid/opus/codex)
    3. Работа + мониторинг
    4. NUDGE если "не закончено" вместо restart
    5. Финальный codex review если много изменений
    """
    
    NUDGE_MESSAGE = "Все пункты ТЗ выполнены? Проверь и заверши работу."
    
    VERIFICATION_PROMPT = """Проверь, выполнена ли задача.

ИСХОДНАЯ ЗАДАЧА: {task}

КРИТЕРИИ ВЫПОЛНЕНИЯ:
{criteria}

ЛОГ ПОСЛЕДНЕЙ РАБОТЫ:
```
{log}
```

Ответь JSON:
{{
    "completed": true/false,
    "quality": "excellent|good|partial|failed",
    "all_criteria_met": true/false,
    "issues": ["список проблем если есть"],
    "summary": "краткий итог"
}}

Только JSON, без комментариев."""

    FINAL_REVIEW_PROMPT = """Ты code reviewer. Проверь изменения на баги и недочёты.

ИСХОДНОЕ ТЗ: {task}

КРИТЕРИИ: {criteria}

ТЗ было выполнено, но нужна проверка качества.
Найди:
- Потенциальные баги
- Проблемы с производительностью
- Недочёты в логике
- Что можно улучшить

Если всё отлично - так и скажи."""

    def __init__(
        self,
        glm_client: GLMClient,
        manager_config: ManagerConfig,
        on_status: Optional[Callable[[str], Awaitable[None]]] = None,
        on_need_human: Optional[Callable[[str], Awaitable[str]]] = None,
    ):
        self.glm = glm_client
        self.config = manager_config
        self.on_status = on_status
        self.on_need_human = on_need_human
        
        self.worker_manager = WorkerManager(
            config=manager_config,
            on_output=self._on_worker_output,
        )
        self.log_watcher = LogWatcher(glm_client)
        self.log_filter = LogFilter()
        self.clarifier = TaskClarifier(glm_client, on_ask_user=on_need_human)
        
        self._current_task: Optional[str] = None
        self._clarified_task: Optional[ClarifiedTask] = None
        self._task_state = TaskState.PENDING
        self._history: List[TaskHistory] = []
        self._accumulated_log: str = ""
        self._nudge_count: int = 0
    
    async def _on_worker_output(self, output: str) -> None:
        """Callback при новом выводе от worker'а"""
        self._accumulated_log += output
    
    async def _report_status(self, message: str) -> None:
        """Сообщить о статусе"""
        logger.info(f"[TaskManager] {message}")
        if self.on_status:
            await self.on_status(message)
    
    async def run_task(
        self,
        task: str,
        worker_type: Optional[WorkerType] = None,  # None = автовыбор
        max_attempts: int = 3,
        max_nudges: int = 3,
        skip_clarification: bool = False,
    ) -> TaskResult:
        """Выполнить задачу с полным циклом
        
        Args:
            task: Задача
            worker_type: Worker (None = автовыбор по сложности)
            max_attempts: Макс попыток (restart'ов)
            max_nudges: Макс nudge'ей перед restart'ом
            skip_clarification: Пропустить уточнение ТЗ
        """
        self._current_task = task
        self._task_state = TaskState.CLARIFYING
        self._history = []
        self._accumulated_log = ""
        self._nudge_count = 0
        
        start_time = asyncio.get_event_loop().time()
        
        # === PHASE 1: Уточнение ТЗ ===
        if not skip_clarification and not self.config.simple_mode:
            await self._report_status("Analyzing task...")
            self._clarified_task = await self.clarifier.clarify(task)
            
            await self._report_status(
                f"Task complexity: {self._clarified_task.complexity.value}, "
                f"{len(self._clarified_task.acceptance_criteria)} criteria"
            )
        else:
            # Быстрая оценка без уточнений
            complexity = await self.clarifier.quick_assess(task)
            self._clarified_task = ClarifiedTask(
                original_task=task,
                clarified_task=task,
                complexity=complexity,
                acceptance_criteria=["Задача выполнена"],
            )
        
        # === PHASE 2: Выбор worker'а ===
        if worker_type is None:
            worker_type = COMPLEXITY_TO_WORKER[self._clarified_task.complexity]
            await self._report_status(f"Auto-selected worker: {worker_type.value}")
        
        # Для SIMPLE (droid) - всегда simple mode
        effective_simple_mode = self.config.simple_mode
        if self._clarified_task.complexity == TaskComplexity.SIMPLE:
            effective_simple_mode = True
            await self._report_status("Simple task → skipping verification")
        
        self._task_state = TaskState.RUNNING
        attempt = 0
        context: Optional[str] = None
        analysis = None
        
        await self._report_status(f"Starting with {worker_type.value} worker")
        
        # === PHASE 3: Работа + мониторинг ===
        while attempt < max_attempts:
            attempt += 1
            self._nudge_count = 0
            
            await self._report_status(f"Attempt {attempt}/{max_attempts}")
            
            # Формируем задачу с критериями
            task_with_criteria = self._format_task_with_criteria()
            
            # Запустить worker
            await self.worker_manager.start_task(task_with_criteria, worker_type, context)
            
            # Для Copilot - ждём завершения напрямую
            if worker_type == WorkerType.OPUS:
                analysis = await self._run_copilot_task()
            else:
                # Для droid/codex - мониторинг с nudge
                analysis = await self._monitor_with_nudge(max_nudges)
            
            # Записать историю
            elapsed = asyncio.get_event_loop().time() - start_time
            self._history.append(TaskHistory(
                attempt=attempt,
                worker_type=worker_type,
                duration=elapsed,
                analysis=analysis,
            ))
            
            await self._report_status(f"[{analysis.result.value}] {analysis.summary}")
            
            # Обработать результат
            if analysis.result == AnalysisResult.COMPLETED:
                break
            
            if analysis.result == AnalysisResult.NEED_HUMAN:
                if self.on_need_human:
                    human_response = await self.on_need_human(analysis.summary)
                    await self.worker_manager.send_message(human_response)
                    continue
                else:
                    await self._report_status("Need human input but no handler")
                    break
            
            # Если stuck/loop/error - restart с контекстом
            if analysis.should_restart:
                context = analysis.context_for_restart
                await self._report_status("Restarting with context...")
                await self.worker_manager.stop()
                self.log_watcher.reset()
                continue
            
            # Fallback - restart
            await self.worker_manager.stop()
            self.log_watcher.reset()
        
        # === PHASE 4: Сбор статистики ===
        input_tokens, output_tokens, cached_tokens = self._collect_token_stats(worker_type)
        
        # === PHASE 5: Верификация ===
        verification_passed = False
        final_summary = ""
        
        if not effective_simple_mode:
            self._task_state = TaskState.VERIFYING
            await self._report_status("Verifying result...")
            verification_passed, final_summary = await self._verify_result()
        else:
            verification_passed = analysis.result == AnalysisResult.COMPLETED
            final_summary = analysis.summary
        
        # === PHASE 6: Финальный review (если много изменений) ===
        if (verification_passed and 
            self._clarified_task.needs_final_review and
            self._clarified_task.complexity == TaskComplexity.COMPLEX):
            
            self._task_state = TaskState.REVIEWING
            await self._report_status("Running final codex review...")
            await self._run_final_review()
        
        # Остановить worker
        await self.worker_manager.stop()
        
        total_time = asyncio.get_event_loop().time() - start_time
        
        # Финальный статус
        if verification_passed:
            self._task_state = TaskState.COMPLETED
        else:
            self._task_state = TaskState.FAILED
        
        return TaskResult(
            task=task,
            state=self._task_state,
            worker_type=worker_type,
            attempts=attempt,
            nudges=self._nudge_count,
            total_time=total_time,
            verification_passed=verification_passed,
            final_summary=final_summary,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            complexity=self._clarified_task.complexity,
            acceptance_criteria=self._clarified_task.acceptance_criteria,
        )
    
    def _format_task_with_criteria(self) -> str:
        """Форматировать задачу с критериями выполнения"""
        if not self._clarified_task:
            return self._current_task
        
        criteria_text = "\n".join(
            f"- {c}" for c in self._clarified_task.acceptance_criteria
        )
        
        return f"""{self._clarified_task.clarified_task}

КРИТЕРИИ ВЫПОЛНЕНИЯ (все должны быть выполнены):
{criteria_text}

Когда закончишь - убедись что ВСЕ критерии выполнены."""
    
    async def _run_copilot_task(self) -> WatcherAnalysis:
        """Запустить copilot и дождаться завершения"""
        await self._report_status("Waiting for copilot to complete...")
        success, output = await self.worker_manager.wait_for_completion(timeout=300)
        self._accumulated_log = output
        
        if success and output.strip():
            output_lower = output.lower()
            if 'error' in output_lower and 'total usage' not in output_lower:
                return WatcherAnalysis(
                    result=AnalysisResult.ERROR,
                    summary="Copilot reported an error",
                    suggestion="Review the error and retry",
                    should_restart=True,
                )
            return WatcherAnalysis(
                result=AnalysisResult.COMPLETED,
                summary=output[:200].replace('\n', ' '),
                suggestion=None,
            )
        
        return WatcherAnalysis(
            result=AnalysisResult.ERROR,
            summary="Copilot timed out or failed",
            suggestion="Retry with different approach",
            should_restart=True,
        )
    
    async def _monitor_with_nudge(self, max_nudges: int) -> WatcherAnalysis:
        """Мониторить worker с nudge вместо restart
        
        Если worker говорит "не закончено" - пинаем его вместо restart.
        """
        while True:
            await asyncio.sleep(self.worker_manager.current_worker.effective_interval)
            
            # Проверить жив ли worker
            if not self.worker_manager.is_running:
                # Сессия умерла - нужен restart
                return WatcherAnalysis(
                    result=AnalysisResult.ERROR,
                    summary="Worker session died",
                    suggestion="Restart worker",
                    should_restart=True,
                )
            
            # Захватить и проанализировать лог
            raw_log = await self.worker_manager.current_worker.capture_output()
            elapsed = self.worker_manager.current_worker.get_elapsed_time()
            
            analysis = await self.log_watcher.analyze(
                raw_log=raw_log,
                task=self._current_task,
                elapsed_seconds=elapsed,
            )
            
            await self._report_status(f"[{analysis.result.value}] {analysis.summary}")
            
            # COMPLETED - отлично
            if analysis.result == AnalysisResult.COMPLETED:
                return analysis
            
            # NEED_HUMAN - передать наверх
            if analysis.result == AnalysisResult.NEED_HUMAN:
                return analysis
            
            # STUCK/LOOP - попробуем nudge
            if analysis.result in (AnalysisResult.STUCK, AnalysisResult.LOOP):
                if self._nudge_count < max_nudges:
                    self._nudge_count += 1
                    self._task_state = TaskState.NUDGING
                    await self._report_status(
                        f"Nudging worker ({self._nudge_count}/{max_nudges})..."
                    )
                    await self.worker_manager.send_message(self.NUDGE_MESSAGE)
                    continue
                else:
                    # Исчерпали nudge'и - restart
                    analysis.should_restart = True
                    return analysis
            
            # ERROR - restart
            if analysis.result == AnalysisResult.ERROR:
                return analysis
            
            # WORKING - продолжаем мониторинг
    
    def _collect_token_stats(self, worker_type: WorkerType) -> tuple[int, int, int]:
        """Собрать статистику токенов"""
        if worker_type == WorkerType.OPUS:
            worker = self.worker_manager.current_worker
            if worker and hasattr(worker, 'token_usage') and worker.token_usage:
                return (
                    worker.token_usage.input_tokens,
                    worker.token_usage.output_tokens,
                    worker.token_usage.cached_tokens,
                )
        return 0, 0, 0
    
    async def _verify_result(self) -> tuple[bool, str]:
        """Верифицировать результат"""
        criteria_text = "\n".join(
            f"- {c}" for c in (self._clarified_task.acceptance_criteria if self._clarified_task else [])
        )
        
        prompt = self.VERIFICATION_PROMPT.format(
            task=self._current_task,
            criteria=criteria_text or "Задача выполнена",
            log=self._accumulated_log[-3000:],  # последние 3000 символов
        )
        
        try:
            result = await self.glm.generate_json(prompt, temperature=0.3)
            completed = result.get("completed", False)
            summary = result.get("summary", "Unknown")
            return completed, summary
        except Exception as e:
            logger.warning(f"Verification failed: {e}")
            return True, "Verification skipped due to error"
    
    async def _run_final_review(self) -> None:
        """Запустить финальный codex review для поиска багов"""
        criteria_text = "\n".join(
            f"- {c}" for c in (self._clarified_task.acceptance_criteria if self._clarified_task else [])
        )
        
        review_task = self.FINAL_REVIEW_PROMPT.format(
            task=self._current_task,
            criteria=criteria_text,
        )
        
        await self._report_status("Codex reviewing for bugs...")
        
        # Запускаем codex для review
        await self.worker_manager.start_task(review_task, WorkerType.CODEX)
        
        # Ждём завершения (codex может работать долго)
        analysis = await self._monitor_with_nudge(max_nudges=2)
        
        if analysis.result == AnalysisResult.COMPLETED:
            await self._report_status("Final review completed")
        else:
            await self._report_status("Final review finished with issues")
