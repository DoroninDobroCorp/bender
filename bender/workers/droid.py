"""
Droid Worker - worker для Droid CLI (простые задачи)

НАДЁЖНАЯ ДЕТЕКЦИЯ:
- Используем `droid exec` с exit code
- Процесс завершается сам когда задача готова
- Fallback на паттерны для интерактивного режима
"""

import asyncio
import logging
import subprocess
from typing import List, Optional, Tuple, Callable, Awaitable

from .base import BaseWorker, WorkerConfig, WorkerStatus

logger = logging.getLogger(__name__)


class DroidWorker(BaseWorker):
    """Worker для Droid CLI
    
    Надёжная детекция завершения:
    1. Non-interactive: `droid exec` завершается сам
    2. Interactive: паттерны + exit code процесса
    """
    
    WORKER_NAME = "droid"
    INTERVAL_MULTIPLIER = 1.0
    
    # Паттерны завершения (для интерактивного режима)
    COMPLETION_PATTERNS = [
        "Task completed",
        "All done", 
        "Successfully",
        "Готово",
        "Changes saved",
        "File updated",
        "## Summary",  # Droid часто выводит summary в конце
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
        self._llm_analyze = llm_analyze
        self._current_task_text = ""
        self._process: Optional[subprocess.Popen] = None
    
    @property
    def cli_command(self) -> List[str]:
        # Всегда используем exec для надёжного завершения
        # --auto high даёт полную автономию
        return [
            "droid", "exec",
            "--auto", "high",
        ]
    
    def format_task(self, task: str, context: Optional[str] = None) -> str:
        self._current_task_text = task
        if context:
            return f"{task}\n\nПредыдущий контекст:\n{context}"
        return task
    
    async def wait_for_completion(self, timeout: float = 600) -> Tuple[bool, str]:
        """Дождаться завершения
        
        Для exec режима - ждём exit code процесса (надёжно!)
        Для interactive - паттерны + LLM fallback
        """
        
        start = asyncio.get_event_loop().time()
        check_interval = 10  # Проверка каждые 10 секунд
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
            
            # 1. НАДЁЖНО: Проверяем завершился ли процесс
            session_alive = await self.is_session_alive()
            if not session_alive:
                self._completed = True
                self._output = current_output
                self.status = WorkerStatus.COMPLETED
                logger.info(f"[{self.WORKER_NAME}] Process exited - task completed")
                return True, self._output
            
            # 2. Детекция по паттернам (для интерактивного режима)
            completion_reason = self.detect_completion(current_output)
            if completion_reason:
                self._completed = True
                self._output = current_output
                self.status = WorkerStatus.COMPLETED
                logger.info(f"[{self.WORKER_NAME}] Pattern: {completion_reason}")
                return True, self._output
            
            # 3. Детекция зависания (300s без изменений)
            if self.detect_stuck(current_output):
                logger.warning(f"[{self.WORKER_NAME}] Stuck - no output for 5min")
                self._completed = True
                self._output = current_output
                self.status = WorkerStatus.STUCK
                return False, self._output
        
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
