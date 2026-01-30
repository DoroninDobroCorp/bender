"""
Codex Worker - worker для Codex CLI (сложные задачи)

НАДЁЖНАЯ ДЕТЕКЦИЯ:
- `codex exec` завершается сам с exit code
- Опционально: --json для JSONL событий
- Fallback на паттерны для интерактивного режима
"""

import asyncio
import logging
from typing import List, Optional, Callable, Awaitable

from .base import BaseWorker, WorkerConfig, WorkerStatus

logger = logging.getLogger(__name__)


class CodexWorker(BaseWorker):
    """Worker для Codex CLI
    
    Надёжная детекция:
    1. Non-interactive: `codex exec` завершается сам
    2. Interactive: паттерны + exit code процесса
    """
    
    WORKER_NAME = "codex"
    INTERVAL_MULTIPLIER = 2.0
    
    # Паттерны завершения (для интерактивного режима)
    COMPLETION_PATTERNS = [
        "Проблем не найдено",
        "No issues found",
        "Task completed",
        "All done",
        "CRITICAL:",
        "HIGH:",
        "Findings:",
        "Summary:",
        "vladimirdoronin@",  # Shell prompt
        "$ exit",
    ]
    
    def __init__(
        self, 
        config: WorkerConfig,
        llm_analyze: Optional[Callable[[str, str, float], Awaitable[dict]]] = None,
    ):
        super().__init__(config)
        self._llm_analyze = llm_analyze
        self._current_task = ""
    
    @property
    def cli_command(self) -> List[str]:
        if self.config.visible:
            # Интерактивный режим
            return [
                "codex",
                "--dangerously-bypass-approvals-and-sandbox",
            ]
        else:
            # Non-interactive: codex exec (завершается сам!)
            return [
                "codex", "exec",
                "--dangerously-bypass-approvals-and-sandbox",
                "--full-auto",
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
    
    async def wait_for_completion(self, timeout: float = 1800) -> tuple:
        """Дождаться завершения
        
        Для exec режима - ждём exit code (надёжно!)
        Для interactive - паттерны + exit code
        """
        
        start = asyncio.get_event_loop().time()
        check_interval = 15  # Проверка каждые 15 секунд
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
            
            # 1. НАДЁЖНО: Проверяем завершился ли процесс
            session_alive = await self.is_session_alive()
            if not session_alive:
                self._completed = True
                self._output = current_output
                self.status = WorkerStatus.COMPLETED
                logger.info(f"[{self.WORKER_NAME}] Process exited - task completed")
                return True, self._output
            
            # 2. Детекция по паттернам
            last_chunk = current_output[-3000:] if len(current_output) > 3000 else current_output
            for pattern in self.COMPLETION_PATTERNS:
                if pattern in last_chunk:
                    self._completed = True
                    self._output = current_output
                    self.status = WorkerStatus.COMPLETED
                    logger.info(f"[{self.WORKER_NAME}] Pattern: '{pattern}'")
                    return True, self._output
            
            # 3. Детекция зависания (300s без изменений)
            if len(current_output) == last_output_len:
                no_change_count += 1
                if no_change_count >= 20:  # 20 * 15s = 300s = 5 минут
                    logger.warning(f"[{self.WORKER_NAME}] No output for 5min - stuck")
                    self._completed = True
                    self._output = current_output
                    self.status = WorkerStatus.STUCK
                    return False, self._output
            else:
                no_change_count = 0
                last_output_len = len(current_output)
        
        # Таймаут
        self._output = current_output if 'current_output' in dir() else ""
        self.status = WorkerStatus.TIMEOUT
        logger.warning(f"[{self.WORKER_NAME}] Timeout after {timeout}s")
        return False, self._output
