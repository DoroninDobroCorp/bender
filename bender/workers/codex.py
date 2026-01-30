"""
Codex Worker - worker для Codex CLI (сложные задачи)

Логика завершения полностью на LLM — она читает логи и решает готово или нет.
Проверка каждые 30 секунд.
"""

import asyncio
import logging
from typing import List, Optional, Callable, Awaitable

from .base import BaseWorker, WorkerConfig, WorkerStatus

logger = logging.getLogger(__name__)


class CodexWorker(BaseWorker):
    """Worker для Codex CLI
    
    Режим для сверхсложных задач: поиск сложных багов, детальное планирование.
    Использует dangerous mode.
    
    Завершение определяется через LLM-анализ логов.
    """
    
    WORKER_NAME = "codex"
    INTERVAL_MULTIPLIER = 2.0
    
    def __init__(
        self, 
        config: WorkerConfig,
        llm_analyze: Optional[Callable[[str, str, float], Awaitable[dict]]] = None,
    ):
        super().__init__(config)
        self._llm_analyze = llm_analyze  # callback для LLM анализа
        self._current_task = ""
    
    @property
    def cli_command(self) -> List[str]:
        return [
            "codex",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
    
    def format_task(self, task: str, context: Optional[str] = None) -> str:
        self._current_task = task
        formatted = f"""ЗАДАЧА:

{task}

Ты ТОЛЬКО НАХОДИШЬ проблемы, НЕ ИСПРАВЛЯЙ их.
Выведи findings в формате:
- CRITICAL/HIGH/MEDIUM/LOW: описание. файл:строка

Если проблем нет — напиши "Проблем не найдено".
"""
        if context:
            formatted += f"\n\nКонтекст:\n{context}"
        return formatted
    
    # Паттерны завершения работы Codex (проверяем в логе)
    COMPLETION_PATTERNS = [
        "Проблем не найдено",
        "No issues found",
        "Task completed",
        "All done",
        "CRITICAL:",  # Нашёл проблемы и вывел
        "HIGH:",
        "Findings:",
        "Summary:",
        "vladimirdoronin@",  # Вернулся в shell prompt
        "$ exit",
        "logout",
    ]
    
    async def wait_for_completion(self, timeout: float = 1800) -> tuple:
        """Дождаться завершения — LLM решает когда готово"""
        
        start = asyncio.get_event_loop().time()
        check_interval = 30  # LLM проверка каждые 30 секунд
        last_output_len = 0
        no_change_count = 0
        
        while asyncio.get_event_loop().time() - start < timeout:
            await asyncio.sleep(check_interval)
            
            # Читаем вывод
            if self._log_file is not None and self._log_file.exists():
                try:
                    current_output = self._log_file.read_text(errors='replace')
                except Exception:
                    current_output = ""
            else:
                current_output = await self.capture_output()
            
            # Детекция завершения по паттернам в логе
            last_chunk = current_output[-3000:] if len(current_output) > 3000 else current_output
            for pattern in self.COMPLETION_PATTERNS:
                if pattern in last_chunk:
                    self._completed = True
                    self._output = current_output
                    self.status = WorkerStatus.COMPLETED
                    logger.info(f"[{self.WORKER_NAME}] Completion pattern found: '{pattern}'")
                    return True, self._output
            
            # Детекция зависания: если лог не меняется 3 раза подряд (90 секунд)
            if len(current_output) == last_output_len:
                no_change_count += 1
                if no_change_count >= 3:
                    logger.warning(f"[{self.WORKER_NAME}] Log unchanged for {no_change_count * check_interval}s, assuming stuck")
                    self._completed = True
                    self._output = current_output
                    self.status = WorkerStatus.STUCK
                    return False, self._output
            else:
                no_change_count = 0
                last_output_len = len(current_output)
            
            # Проверяем жива ли сессия
            session_alive = await self.is_session_alive()
            if not session_alive:
                self._completed = True
                self._output = current_output
                self.status = WorkerStatus.COMPLETED
                logger.info(f"[{self.WORKER_NAME}] Session closed, completed")
                return True, self._output
            
            # LLM анализ если есть callback
            if self._llm_analyze and len(current_output) > 100:
                elapsed = asyncio.get_event_loop().time() - start
                try:
                    analysis = await self._llm_analyze(
                        current_output[-8000:],  # последние 8k символов
                        self._current_task,
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
        
        # Таймаут
        self._output = current_output if 'current_output' in dir() else ""
        self.status = WorkerStatus.TIMEOUT
        logger.warning(f"[{self.WORKER_NAME}] Timeout after {timeout}s")
        return False, self._output
