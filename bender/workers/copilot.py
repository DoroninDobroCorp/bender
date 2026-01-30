"""
Copilot Worker - основной worker для GitHub Copilot CLI
"""

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple, Callable, Awaitable

from .base import BaseWorker, WorkerConfig, WorkerStatus

logger = logging.getLogger(__name__)


def cleanup_copilot_state() -> None:
    """Очистить состояние copilot для чистого старта
    
    Удаляет session-state и command-history чтобы каждый
    запуск bender был с чистого листа.
    """
    copilot_dir = Path.home() / ".copilot"
    if not copilot_dir.exists():
        return
    
    # Очистить session-state (старые сессии)
    session_state = copilot_dir / "session-state"
    if session_state.exists():
        try:
            shutil.rmtree(session_state)
            session_state.mkdir()
            logger.info("[copilot] Cleared session-state")
        except Exception as e:
            logger.warning(f"[copilot] Failed to clear session-state: {e}")
    
    # Очистить command-history (может влиять на контекст)
    history_file = copilot_dir / "command-history-state.json"
    if history_file.exists():
        try:
            history_file.write_text("{}")
            logger.info("[copilot] Cleared command-history")
        except Exception as e:
            logger.warning(f"[copilot] Failed to clear command-history: {e}")


@dataclass
class TokenUsage:
    """Статистика использования токенов"""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    api_time_seconds: float = 0.0
    total_time_seconds: float = 0.0
    model: str = ""
    premium_requests: int = 0
    
    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens
    
    def __str__(self) -> str:
        return (
            f"Tokens: {self.input_tokens:,} in / {self.output_tokens:,} out "
            f"({self.cached_tokens:,} cached) | "
            f"Time: {self.api_time_seconds:.1f}s API / {self.total_time_seconds:.1f}s total"
        )


class CopilotWorker(BaseWorker):
    """Worker для GitHub Copilot CLI
    
    Основной режим работы (opus). Используется для большинства задач.
    Запускает `copilot -p "task"` в non-interactive режиме.
    
    В отличие от интерактивных CLI, copilot с -p выполняет задачу и завершается.
    """
    
    WORKER_NAME = "copilot"
    INTERVAL_MULTIPLIER = 1.0
    STARTUP_DELAY = 1.0
    
    # Паттерны завершения специфичные для Copilot
    COMPLETION_PATTERNS = [
        "Task completed",
        "All done",
        "Successfully",
        "Готово",
        "Total usage est:",  # Статистика в конце
        "API time spent:",   # Статистика в конце
        "Premium request",   # Статистика в конце
    ]
    
    # Паттерны для парсинга статистики из вывода copilot
    TOKEN_PATTERN = re.compile(
        r'(\w[\w\-\.]+)\s+([\d.]+)k\s+in,\s+([\d.]+)\s+out,\s+([\d.]+)k\s+cached'
    )
    API_TIME_PATTERN = re.compile(r'API time spent:\s+(\d+)s')
    TOTAL_TIME_PATTERN = re.compile(r'Total session time:\s+(\d+)s')
    PREMIUM_PATTERN = re.compile(r'(\d+)\s+Premium request')
    
    _state_cleaned = False  # Class-level flag to clean only once per process
    
    def __init__(
        self, 
        config: WorkerConfig, 
        model: str = "claude-sonnet-4", 
        visible: bool = False,
        llm_analyze: Optional[Callable[[str, str, float], Awaitable[dict]]] = None,
    ):
        super().__init__(config)
        self.model = model
        self.visible = visible
        self._pending_task: Optional[str] = None
        self._output: str = ""
        self._completed: bool = False
        self.token_usage: Optional[TokenUsage] = None
        self._llm_analyze = llm_analyze
        self._current_task_text = ""
        
        # Очистить состояние copilot один раз при первом создании worker'а
        if not CopilotWorker._state_cleaned:
            cleanup_copilot_state()
            CopilotWorker._state_cleaned = True
    
    @property
    def cli_command(self) -> List[str]:
        """CLI команда для copilot"""
        cmd = [
            "copilot",
            "--allow-all",  # tools + paths + urls - никаких вопросов
            "--model", self.model,
        ]
        # Non-interactive mode с задачей
        if self._pending_task:
            cmd.extend(["-p", self._pending_task])
        return cmd
    
    def format_task(self, task: str, context: Optional[str] = None) -> str:
        """Форматировать задачу для copilot"""
        self._current_task_text = task  # Сохраняем для LLM анализа
        if context:
            full_task = f"{task}\n\nКонтекст предыдущей работы:\n{context}"
        else:
            full_task = task
        self._pending_task = full_task
        return full_task
    
    async def start(self, task: str, context: Optional[str] = None) -> None:
        """Запустить copilot с задачей
        
        Copilot в режиме -p выполняет задачу и завершается.
        В visible mode запускается в native Terminal.app для удобного чтения.
        """
        self.current_task = task
        self.status = WorkerStatus.RUNNING
        self.start_time = __import__('time').time()
        self._output = ""
        self._completed = False
        
        # Форматируем задачу (это также устанавливает _pending_task для cli_command)
        formatted_task = self.format_task(task, context)
        logger.info(f"[{self.WORKER_NAME}] Starting: {task[:50]}...")
        
        if self.visible:
            # Visible mode - используем native Terminal.app (как droid)
            await self._start_native_terminal(formatted_task)
        else:
            # Background mode - subprocess
            cmd = self.cli_command
            logger.debug(f"[{self.WORKER_NAME}] Command: {cmd}")
            await self._start_background(cmd)
    
    async def _start_background(self, cmd: List[str]) -> None:
        """Запустить в фоне через subprocess"""
        try:
            logger.info(f"[{self.WORKER_NAME}] Running: {' '.join(cmd)}")
            logger.info(f"[{self.WORKER_NAME}] CWD: {self.config.project_path}")
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.config.project_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            logger.info(f"[{self.WORKER_NAME}] Process started (PID: {self._process.pid})")
        except Exception as e:
            logger.error(f"[{self.WORKER_NAME}] Failed to start: {e}")
            self.status = WorkerStatus.ERROR
            raise
    
    async def capture_output(self) -> str:
        """Захватить вывод от copilot"""
        if self._process is None:
            return self._output
        
        # Читаем доступный вывод
        try:
            # Non-blocking read
            if self._process.stdout:
                try:
                    chunk = await asyncio.wait_for(
                        self._process.stdout.read(4096),
                        timeout=0.5
                    )
                    if chunk:
                        self._output += chunk.decode('utf-8', errors='replace')
                except asyncio.TimeoutError:
                    pass
        except Exception as e:
            logger.warning(f"[{self.WORKER_NAME}] Error reading output: {e}")
        
        return self._output
    
    async def is_session_alive(self) -> bool:
        """Проверить, работает ли copilot"""
        # Visible mode - используем базовый метод для native terminal
        if self.visible:
            return await super().is_session_alive()
        
        if self._process is None:
            return False
        
        # Check if process is still running
        if self._process.returncode is not None:
            self._completed = True
            return False
        
        return True
    
    async def wait_for_completion(self, timeout: float = 300) -> Tuple[bool, str]:
        """Дождаться завершения copilot
        
        Returns:
            Tuple[success, output]
        """
        # Visible mode - используем логику с маркерами
        if self.visible:
            return await self._wait_visible(timeout)
        
        if self._process is None:
            return False, ""
        
        try:
            stdout, _ = await asyncio.wait_for(
                self._process.communicate(),
                timeout=timeout
            )
            self._output = stdout.decode('utf-8', errors='replace') if stdout else ""
            self._completed = True
            self.status = WorkerStatus.COMPLETED
            
            # Парсим статистику токенов
            self.token_usage = self._parse_token_usage(self._output)
            if self.token_usage:
                logger.info(f"[{self.WORKER_NAME}] {self.token_usage}")
            
            logger.info(f"[{self.WORKER_NAME}] Completed with {len(self._output)} chars output")
            return True, self._output
        except asyncio.TimeoutError:
            logger.warning(f"[{self.WORKER_NAME}] Timeout after {timeout}s")
            self.status = WorkerStatus.STUCK
            return False, self._output
        except Exception as e:
            logger.error(f"[{self.WORKER_NAME}] Error waiting: {e}")
            self.status = WorkerStatus.ERROR
            return False, str(e)
    
    async def _wait_visible(self, timeout: float) -> Tuple[bool, str]:
        """Дождаться завершения в visible mode — LLM решает когда готово"""
        start = asyncio.get_event_loop().time()
        check_interval = 30  # LLM проверка каждые 30 секунд
        current_output = ""
        
        while asyncio.get_event_loop().time() - start < timeout:
            await asyncio.sleep(check_interval)
            elapsed = asyncio.get_event_loop().time() - start
            
            # Читаем текущий лог
            if self._log_file is not None and self._log_file.exists():
                try:
                    current_output = self._log_file.read_text(errors='replace')
                except Exception:
                    current_output = ""
            else:
                current_output = ""
            
            # 1. Детекция по паттернам (быстрая, без LLM)
            completion_reason = self.detect_completion(current_output)
            if completion_reason:
                self._completed = True
                self._output = current_output
                self.status = WorkerStatus.COMPLETED
                self.token_usage = self._parse_token_usage(self._output)
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
                self.token_usage = self._parse_token_usage(self._output)
                logger.info(f"[{self.WORKER_NAME}] Session closed, completed")
                return True, self._output
            
            # 4. LLM анализ (если есть)
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
                        self.token_usage = self._parse_token_usage(self._output)
                        logger.info(f"[{self.WORKER_NAME}] LLM says completed: {analysis.get('summary', '')}")
                        return True, self._output
                    
                    if status == "error":
                        self._completed = False
                        self._output = current_output
                        self.status = WorkerStatus.ERROR
                        logger.warning(f"[{self.WORKER_NAME}] LLM detected error")
                        return False, self._output
                        
                except Exception as e:
                    logger.debug(f"LLM analyze failed: {e}")
        
        logger.warning(f"[{self.WORKER_NAME}] Timeout in visible mode after {timeout}s")
        self.status = WorkerStatus.STUCK
        return False, current_output
    
    async def stop(self) -> None:
        """Остановить copilot"""
        # Visible mode - используем базовый метод для закрытия native terminal
        if self.visible:
            await self._close_native_terminal()
            return
        
        # Background mode - убиваем процесс
        if self._process and self._process.returncode is None:
            logger.info(f"[{self.WORKER_NAME}] Terminating process")
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
        
        self._process = None
        self.status = WorkerStatus.IDLE
        self.current_task = None
    
    def _parse_token_usage(self, output: str) -> Optional[TokenUsage]:
        """Парсить статистику токенов из вывода copilot
        
        Пример вывода:
        Total usage est:        1 Premium request
        API time spent:         6s
        Total session time:     9s
        Breakdown by AI model:
         claude-sonnet-4         31.9k in, 302 out, 26.0k cached (Est. 1 Premium request)
        """
        try:
            usage = TokenUsage()
            
            # Парсим токены модели
            token_match = self.TOKEN_PATTERN.search(output)
            if token_match:
                usage.model = token_match.group(1)
                usage.input_tokens = int(float(token_match.group(2)) * 1000)
                usage.output_tokens = int(float(token_match.group(3)))
                usage.cached_tokens = int(float(token_match.group(4)) * 1000)
            
            # API time
            api_match = self.API_TIME_PATTERN.search(output)
            if api_match:
                usage.api_time_seconds = float(api_match.group(1))
            
            # Total time
            total_match = self.TOTAL_TIME_PATTERN.search(output)
            if total_match:
                usage.total_time_seconds = float(total_match.group(1))
            
            # Premium requests
            premium_match = self.PREMIUM_PATTERN.search(output)
            if premium_match:
                usage.premium_requests = int(premium_match.group(1))
            
            # Если ничего не нашли - вернуть None
            if usage.input_tokens == 0 and usage.output_tokens == 0:
                return None
            
            return usage
            
        except Exception as e:
            logger.warning(f"[{self.WORKER_NAME}] Failed to parse token usage: {e}")
            return None
