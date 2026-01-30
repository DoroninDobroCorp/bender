"""
Droid Worker - worker для Droid CLI (простые задачи)

Логика завершения на LLM — она читает логи и решает готово или нет.
Проверка каждые 30 секунд.
"""

import asyncio
import logging
from typing import List, Optional, Tuple, Callable, Awaitable

from .base import BaseWorker, WorkerConfig, WorkerStatus

logger = logging.getLogger(__name__)


class DroidWorker(BaseWorker):
    """Worker для Droid CLI
    
    Режим для простых задач. Droid использует модель Sonnet.
    Завершение определяется через LLM-анализ логов.
    """
    
    WORKER_NAME = "droid"
    INTERVAL_MULTIPLIER = 1.0
    
    # Паттерны завершения специфичные для Droid
    COMPLETION_PATTERNS = [
        "Task completed",
        "All done",
        "Successfully",
        "Готово",
        "Завершено",
        "Changes saved",
        "File updated",
    ]
    
    def __init__(
        self, 
        config: WorkerConfig, 
        llm_check_completion: Optional[Callable[[str, str], Awaitable[bool]]] = None,
        llm_analyze: Optional[Callable[[str, str, float], Awaitable[dict]]] = None,
    ):
        super().__init__(config)
        self._output: str = ""
        self._completed: bool = False
        self._llm_check_completion = llm_check_completion  # Deprecated, для совместимости
        self._llm_analyze = llm_analyze  # LLM анализ логов
        self._current_task_text = ""
    
    @property
    def cli_command(self) -> List[str]:
        # Visible режим: интерактивный droid с TUI (пользователь видит что происходит)
        # Background режим: droid exec (без TUI, для логов)
        if self.config.visible:
            return ["droid", "--auto", "high"]
        else:
            return ["droid", "exec", "--skip-permissions-unsafe"]
    
    def format_task(self, task: str, context: Optional[str] = None) -> str:
        self._current_task_text = task
        if context:
            return f"{task}\n\nПредыдущий контекст:\n{context}"
        return task
    
    async def wait_for_completion(self, timeout: float = 600) -> Tuple[bool, str]:
        """Дождаться завершения — LLM решает когда готово"""
        
        start = asyncio.get_event_loop().time()
        check_interval = 30  # LLM проверка каждые 30 секунд
        current_output = ""
        
        while asyncio.get_event_loop().time() - start < timeout:
            await asyncio.sleep(check_interval)
            elapsed = asyncio.get_event_loop().time() - start
            
            # Читаем вывод
            if self._log_file is not None and self._log_file.exists():
                try:
                    current_output = self._log_file.read_text(errors='replace')
                except Exception:
                    current_output = ""
            else:
                current_output = await self.capture_output()
            
            # 1. Детекция по паттернам (быстрая, без LLM)
            completion_reason = self.detect_completion(current_output)
            if completion_reason:
                self._completed = True
                self._output = current_output
                self.status = WorkerStatus.COMPLETED
                logger.info(f"[{self.WORKER_NAME}] Completed: {completion_reason}")
                return True, self._output
            
            # 2. Детекция зависания
            if self.detect_stuck(current_output):
                logger.warning(f"[{self.WORKER_NAME}] Stuck detected (no output change)")
                self._completed = True
                self._output = current_output
                self.status = WorkerStatus.STUCK
                return False, self._output
            
            # 3. Проверяем жива ли сессия
            session_alive = await self.is_session_alive()
            if not session_alive:
                self._completed = True
                self._output = current_output
                self.status = WorkerStatus.COMPLETED
                logger.info(f"[{self.WORKER_NAME}] Session closed, completed")
                return True, self._output
            
            # 4. LLM анализ (если есть и паттерны не сработали)
            if self._llm_analyze and len(current_output) > 100:
                try:
                    analysis = await self._llm_analyze(
                        current_output[-6000:],
                        self._current_task_text,
                        elapsed
                    )
                    
                    status = analysis.get("status", "working")
                    
                    if status == "completed":
                        self._completed = True
                        self._output = current_output
                        self.status = WorkerStatus.COMPLETED
                        logger.info(f"[{self.WORKER_NAME}] LLM says completed: {analysis.get('summary', '')}")
                        return True, self._output
                    
                    if status == "error":
                        self._completed = False
                        self._output = current_output
                        self.status = WorkerStatus.ERROR
                        logger.warning(f"[{self.WORKER_NAME}] LLM detected error: {analysis.get('summary', '')}")
                        return False, self._output
                        
                except Exception as e:
                    logger.debug(f"LLM analyze failed: {e}")
            
            # Fallback на старый callback если есть
            elif self._llm_check_completion and len(current_output) > 100:
                try:
                    is_done = await self._llm_check_completion(self._current_task_text, current_output[-3000:])
                    if is_done:
                        self._completed = True
                        self._output = current_output
                        self.status = WorkerStatus.COMPLETED
                        logger.info(f"[{self.WORKER_NAME}] LLM confirmed completion")
                        return True, self._output
                except Exception as e:
                    logger.debug(f"LLM check failed: {e}")
        
        # Таймаут
        self._output = current_output
        self.status = WorkerStatus.TIMEOUT
        logger.warning(f"[{self.WORKER_NAME}] Timeout after {timeout}s")
        return False, self._output
    
    @property
    def output(self) -> str:
        return self._output
    
    @property
    def completed(self) -> bool:
        return self._completed
