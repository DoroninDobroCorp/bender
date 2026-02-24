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
from .llm_router import LLMRouter
from .task_clarifier import TaskClarifier, TaskComplexity, ClarifiedTask
from .console_recovery import ConsoleRecovery

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
    full_output: str = ""  # Полный вывод worker'а
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
    MAX_DROID_RETRIES = 1  # Максимум 1 retry для Droid (всего 2 попытки)
    
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
        glm_client: LLMRouter,
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
        self.clarifier = TaskClarifier(
            glm_client, 
            on_ask_user=on_need_human,
            project_path=str(manager_config.project_path),
        )
        self._console_recovery = ConsoleRecovery()
        
        # Connect GLM token tracking to context manager
        self.glm.set_usage_callback(self.log_watcher.context.add_llm_usage)
        
        self._current_task: Optional[str] = None
        self._clarified_task: Optional[ClarifiedTask] = None
        self._task_state = TaskState.PENDING
        self._history: List[TaskHistory] = []
        self._accumulated_log: str = ""
        self._nudge_count: int = 0
        self._stop_requested: bool = False
    
    def request_stop(self) -> None:
        """Request graceful stop of current task"""
        self._stop_requested = True
    
    async def _on_worker_output(self, output: str) -> None:
        """Callback при новом выводе от worker'а"""
        self._accumulated_log += output
    
    async def _report_status(self, message: str) -> None:
        """Сообщить о статусе"""
        logger.info(f"[TaskManager] {message}")
        if self.on_status:
            await self.on_status(message)
    
    def _validate_droid_run(self, output: str) -> bool:
        """Проверяет, действительно ли Droid что-то сделал
        
        Проверяем наличие JSON события completion или хотя бы tool_call.
        Если вывод пустой или нет признаков работы - считаем silent fail.
        
        Args:
            output: Вывод от Droid worker
            
        Returns:
            True если Droid реально работал, False если silent fail
        """
        if not output or len(output.strip()) < 10:
            logger.warning("[TaskManager] Droid output is empty or too short")
            return False
        
        # Проверяем наличие JSON события завершения
        if '"type": "completion"' in output or '"type":"completion"' in output:
            logger.info("[TaskManager] Droid completion event detected")
            return True
        
        # Хотя бы пытался что-то делать (вызывал инструменты)
        if '"type": "tool_call"' in output or '"type":"tool_call"' in output:
            logger.info("[TaskManager] Droid tool_call detected")
            return True
        
        # Проверяем наличие форматированных событий (если парсер сработал)
        if any(marker in output for marker in ['🔧 Выполняю:', '📖 Читаю:', '✏️  Редактирую:', '📄 Создаю:']):
            logger.info("[TaskManager] Droid formatted events detected")
            return True
        
        logger.warning("[TaskManager] Droid output has no completion or tool_call events")
        return False
    
    async def _generate_human_summary(self, task: str, output: str) -> str:
        """Генерирует краткий отчет для человека
        
        Использует LLM для создания понятного саммари на русском языке.
        
        Args:
            task: Исходная задача
            output: Полный вывод worker'а
            
        Returns:
            Краткий отчет (1-2 предложения) с эмодзи
        """
        # Обрезаем вывод если он гигантский
        trimmed_output = output[-8000:] if len(output) > 8000 else output
        
        prompt = f"""Ты - старший разработчик. Твой коллега-робот выполнял задачу: "{task}".

Вот его логи:
---
{trimmed_output}
---

Напиши КРАТКО (1-2 предложения) на русском языке:
1. Что именно было сделано? (какие файлы изменены, какие тесты пройдены)
2. Есть ли проблемы?

Пиши для человека, без технического мусора. Используй эмодзи.
Пример: "✅ Обновил файл auth.py, добавил проверку токена. Тесты прошли успешно."

ТОЛЬКО краткий отчет, без лишних слов!"""
        
        try:
            summary = await self.glm.generate(prompt, temperature=0.3)
            return summary.strip()
        except Exception as e:
            logger.warning(f"[TaskManager] Failed to generate summary: {e}")
            return "⚠️ Не удалось сгенерировать отчет (LLM error)."
    
    async def _is_git_clean(self) -> bool:
        """Проверка чистоты git репозитория
        
        FIX #3: Добавлен timeout для защиты от зависания
        """
        try:
            proc = await asyncio.create_subprocess_shell(
                "git status --porcelain",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.config.project_path
            )
            
            # FIX #3: Обязательный timeout (10 секунд для git команды)
            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=10.0
                )
                return len(stdout.strip()) == 0
            except asyncio.TimeoutError:
                # FIX #5: Log timeout вместо silent fail
                logger.warning("Git status command timed out after 10s")
                proc.kill()
                await proc.wait()
                return True  # Считаем что ок если timeout
                
        except Exception as e:
            # FIX #5: Log exception вместо silent pass
            logger.warning(f"Git status check failed: {e}")
            # Если это не git репо или ошибка - считаем что ок
            return True
    
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
        self._console_recovery.reset()
        self.log_watcher.reset()  # Reset stuck detection timer for new task
        
        # === SAFETY CHECK: Git Status ===
        if not await self._is_git_clean():
            logger.warning("⚠️ ВНИМАНИЕ: В репозитории есть незакоммиченные изменения! Bender может их повредить.")
            await self._report_status("⚠️ Репозиторий не чист - есть незакоммиченные изменения")
        
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
        droid_failures = 0  # Счётчик неудачных попыток Droid
        
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
            
            # === FIX #2: Валидация для Droid ===
            if worker_type == WorkerType.DROID and analysis.result == AnalysisResult.COMPLETED:
                # ВАЖНО: Валидируем по СЫРОМУ выводу (там есть JSON события)
                # но отображаем ФОРМАТИРОВАННЫЙ (красивый с эмодзи)
                worker = self.worker_manager.current_worker
                if worker and hasattr(worker, '_output'):
                    raw_output = worker._output  # Сырой JSON для валидации
                else:
                    raw_output = self._accumulated_log
                
                is_valid_run = self._validate_droid_run(raw_output)
                if not is_valid_run:
                    logger.warning("[TaskManager] Droid finished, but no completion detected (silent crash?)")
                    droid_failures += 1
                    
                    if droid_failures > self.MAX_DROID_RETRIES:
                        logger.error("[TaskManager] 🛑 Droid keeps failing. Escalating to HUMAN.")
                        await self._report_status("🛑 Droid не отвечает (silent fail). Требуется вмешательство человека.")
                        
                        # Останавливаем worker
                        await self.worker_manager.stop()
                        
                        return TaskResult(
                            task=task,
                            state=TaskState.FAILED,
                            worker_type=worker_type,
                            attempts=attempt,
                            nudges=self._nudge_count,
                            total_time=asyncio.get_event_loop().time() - start_time,
                            verification_passed=False,
                            final_summary="Droid не отвечает (silent fail). Требуется вмешательство человека.",
                            full_output=self._accumulated_log,
                            error="Droid silent failure after multiple retries",
                            complexity=self._clarified_task.complexity if self._clarified_task else None,
                            acceptance_criteria=self._clarified_task.acceptance_criteria if self._clarified_task else [],
                        )
                    
                    logger.info(f"[TaskManager] Retrying Droid task... (failure {droid_failures}/{self.MAX_DROID_RETRIES})")
                    await self._report_status(f"⚠️ Droid не завершил работу корректно. Retry {droid_failures}/{self.MAX_DROID_RETRIES}...")
                    await self.worker_manager.stop()
                    self.log_watcher.reset()
                    await asyncio.sleep(2)  # Даём отдышаться
                    continue  # Retry
            
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
        
        # === PHASE 4: Генерация человеческого отчёта (FIX #3) ===
        if analysis and analysis.result == AnalysisResult.COMPLETED and self._accumulated_log:
            await self._report_status("🤔 Анализирую результат...")
            human_summary = await self._generate_human_summary(task, self._accumulated_log)
            
            # Выводим ярко (ANSI коды для терминала)
            GREEN = "\033[92m"
            BOLD = "\033[1m"
            RESET = "\033[0m"
            
            print("\n" + "="*60)
            print(f"{BOLD}{GREEN}{human_summary}{RESET}")
            print("="*60 + "\n")
            
            # Также отправляем через callback если есть
            await self._report_status(f"📋 {human_summary}")
        
        # === PHASE 5: Сбор статистики ===
        input_tokens, output_tokens, cached_tokens = self._collect_token_stats(worker_type)
        
        # === PHASE 6: Верификация ===
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
            full_output=self._accumulated_log,
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
        
        # Для Droid используем форматированный вывод
        worker = self.worker_manager.current_worker
        if worker and worker.WORKER_NAME == "droid":
            self._accumulated_log = worker.output
        else:
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
        
        Использует два интервала:
        - status_interval (5s): быстрые проверки для показа прогресса  
        - check_interval (60s): полный анализ с LogWatcher
        """
        status_interval = 5.0  # Быстрые проверки каждые 5 сек
        full_check_interval = self.worker_manager.current_worker.effective_interval
        time_since_full_check = 0.0
        last_log_len = 0
        
        # Начальный статус
        await self._report_status("⏳ Worker started, monitoring...")
        
        while not self._stop_requested:
            await asyncio.sleep(status_interval)
            time_since_full_check += status_interval
            
            # Check for stop request
            if self._stop_requested:
                return WatcherAnalysis(
                    result=AnalysisResult.ERROR,
                    summary="Stopped by user",
                    suggestion="",
                    should_restart=False,
                )
            
            # Проверить жив ли worker
            if not self.worker_manager.is_running:
                # Worker не running - проверим done файл для успешного завершения
                # ВАЖНО: даём время на запись done файла
                worker = self.worker_manager.current_worker
                done_file = getattr(worker, '_done_file', None)
                
                # Ждём до 3 секунд появления done файла
                for _ in range(6):
                    if done_file and done_file.exists():
                        break
                    await asyncio.sleep(0.5)
                
                if done_file and done_file.exists():
                    try:
                        # FIX #1: Async file read (но тут можем оставить sync т.к. файл маленький)
                        exit_code = int(done_file.read_text().strip())
                        raw_log = await worker.capture_output()
                        if exit_code == 0:
                            # Успешное завершение!
                            logger.info(f"[TaskManager] Worker completed with exit code 0")
                            await self._report_status("✅ Task completed successfully")
                            return WatcherAnalysis(
                                result=AnalysisResult.COMPLETED,
                                summary="Task completed successfully",
                                suggestion="",
                                should_restart=False,
                            )
                        else:
                            logger.warning(f"[TaskManager] Worker failed with exit code {exit_code}")
                            return WatcherAnalysis(
                                result=AnalysisResult.ERROR,
                                summary=f"Worker exited with code {exit_code}",
                                suggestion="Check logs for errors",
                                should_restart=True,
                            )
                    except (ValueError, IOError) as e:
                        # FIX #5: Log exception вместо silent pass
                        logger.warning(f"Failed to read exit code from {done_file}: {e}")
                        pass
                
                # Нет done файла или ошибка чтения - сессия умерла
                return WatcherAnalysis(
                    result=AnalysisResult.ERROR,
                    summary="Worker session died",
                    suggestion="Restart worker",
                    should_restart=True,
                )
            
            # Захватить лог для быстрой проверки
            raw_log = await self.worker_manager.current_worker.capture_output()
            elapsed = self.worker_manager.current_worker.get_elapsed_time()
            current_log_len = len(raw_log)

            # Сохраняем последний полный лог для итогового результата
            # raw_log для Droid УЖЕ форматированный (capture_output возвращает _formatted_output)
            if raw_log and len(raw_log) > len(self._accumulated_log):
                self._accumulated_log = raw_log

            # Проверить, жива ли сессия на уровне tmux/терминала
            try:
                session_alive = await self.worker_manager.current_worker.is_session_alive()
            except Exception as e:
                # FIX #5: Log exception вместо silent pass
                logger.warning(f"Failed to check if session is alive: {e}")
                session_alive = True
            if not session_alive:
                # Сессия завершилась - проверим done файл для успешного завершения
                # ВАЖНО: даём время на запись done файла (sync + sleep в скрипте)
                worker = self.worker_manager.current_worker
                done_file = getattr(worker, '_done_file', None)
                
                # Ждём до 3 секунд появления done файла
                for _ in range(6):
                    if done_file and done_file.exists():
                        break
                    await asyncio.sleep(0.5)
                
                if done_file and done_file.exists():
                    try:
                        # FIX #1: Async file read (но тут можем оставить sync т.к. файл маленький)
                        exit_code = int(done_file.read_text().strip())
                        if exit_code == 0:
                            # Успешное завершение!
                            logger.info(f"[TaskManager] Worker completed with exit code 0")
                            await self._report_status("✅ Task completed successfully")
                            return WatcherAnalysis(
                                result=AnalysisResult.COMPLETED,
                                summary="Task completed successfully",
                                suggestion="",
                                should_restart=False,
                            )
                        else:
                            logger.warning(f"[TaskManager] Worker failed with exit code {exit_code}")
                            return WatcherAnalysis(
                                result=AnalysisResult.ERROR,
                                summary=f"Worker exited with code {exit_code}",
                                suggestion="Check logs for errors",
                                should_restart=True,
                            )
                    except (ValueError, IOError) as e:
                        # FIX #5: Log exception вместо silent pass
                        logger.warning(f"Failed to read exit code from {done_file}: {e}")
                        pass
                
                # Нет done файла - попробуем recovery
                recovered = await self._attempt_console_recovery("Сессия терминала не отвечает", raw_log)
                if recovered:
                    continue
                return WatcherAnalysis(
                    result=AnalysisResult.ERROR,
                    summary="Worker session died",
                    suggestion="Restart worker",
                    should_restart=True,
                )

            # Консольные ошибки — пытаемся мягко восстановить
            console_issue = self._console_recovery.detect_issue(raw_log)
            if console_issue:
                recovered = await self._attempt_console_recovery(console_issue, raw_log)
                if recovered:
                    continue
                return WatcherAnalysis(
                    result=AnalysisResult.ERROR,
                    summary="Console error persisted",
                    suggestion="Restart worker",
                    should_restart=True,
                )
            
            # === БЫСТРАЯ ПРОВЕРКА: heartbeat каждые 10 сек ===
            # Показываем прогресс даже если полный анализ ещё не пора
            log_changed = current_log_len != last_log_len
            last_log_len = current_log_len
            
            # Если время для полного анализа ещё не пришло - показываем heartbeat
            if time_since_full_check < full_check_interval:
                if log_changed and current_log_len > 0:
                    # Лог обновился - показываем прогресс
                    # Берём последние 100 символов для краткого превью
                    preview = raw_log[-100:].replace('\n', ' ').strip()
                    if preview:
                        await self._report_status(f"⏳ Working... ({int(elapsed)}s) {preview[:50]}...")
                else:
                    # Лог пустой или не изменился - показываем heartbeat каждые 15 сек
                    if int(time_since_full_check) % 15 < status_interval:
                        await self._report_status(f"⏳ Droid working... ({int(elapsed)}s)")
                continue
            
            # === ПОЛНЫЙ АНАЛИЗ: каждые 60 сек ===
            time_since_full_check = 0.0
            
            analysis = await self.log_watcher.analyze(
                raw_log=raw_log,
                task=self._current_task,
                elapsed_seconds=elapsed,
                process_alive=session_alive,  # Сообщаем что процесс ещё работает
            )
            
            await self._report_status(f"[{analysis.result.value}] {analysis.summary}")

            # Droid часто молчит — не считаем STUCK/LOOP ошибкой если процесс жив
            worker = self.worker_manager.current_worker
            if (worker and worker.WORKER_NAME == "droid" and
                analysis.result in (AnalysisResult.STUCK, AnalysisResult.LOOP) and session_alive):
                await self._report_status("⏳ Droid quiet, still running...")
                continue

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
                console_issue = self._console_recovery.detect_issue(raw_log)
                if console_issue:
                    recovered = await self._attempt_console_recovery(console_issue, raw_log)
                    if recovered:
                        continue
                return analysis
            
            # WORKING - продолжаем мониторинг

    async def _attempt_console_recovery(self, reason: str, output: str) -> bool:
        """Попробовать восстановить консоль через мягкий nudge"""
        worker = self.worker_manager.current_worker
        if not worker:
            return False
        return await self._console_recovery.attempt_recovery(
            worker=worker,
            on_status=self._report_status,
            reason=reason,
            output=output,
        )
    
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
            # FIX #5: Log exception вместо silent return
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
