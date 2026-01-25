"""
Base Worker - абстрактный класс для CLI workers
"""

import asyncio
import logging
import shlex
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional, List, Callable, Awaitable
import uuid

logger = logging.getLogger(__name__)


class WorkerStatus(str, Enum):
    """Статус worker'а"""
    IDLE = "idle"           # Ожидает задачу
    RUNNING = "running"     # Выполняет задачу
    COMPLETED = "completed" # Задача выполнена
    STUCK = "stuck"         # Завис
    LOOP = "loop"           # Зациклился
    ERROR = "error"         # Ошибка
    NEED_HUMAN = "need_human"  # Нужен человек


@dataclass
class WorkerResult:
    """Результат работы worker'а"""
    status: WorkerStatus
    task: str
    output: str = ""
    error: Optional[str] = None
    duration_seconds: float = 0.0
    retries: int = 0
    context_passed: bool = False  # Передавался ли контекст при перезапуске


@dataclass
class WorkerConfig:
    """Конфигурация worker'а"""
    project_path: Path
    check_interval: float = 30.0  # Как часто проверять логи
    visible: bool = False         # Показывать терминал
    simple_mode: bool = False     # Без перепроверки
    max_retries: int = 3          # Максимум перезапусков
    stuck_timeout: float = 300.0  # Таймаут на зависание (5 мин)


class BaseWorker(ABC):
    """Базовый класс для CLI workers
    
    Workers запускают CLI инструменты (copilot, droid, codex) в tmux сессиях
    и следят за их выполнением.
    """
    
    WORKER_NAME: str = "base"
    INTERVAL_MULTIPLIER: float = 1.0  # Для codex = 2.0
    
    STARTUP_DELAY: float = 2.0  # Время на загрузку CLI перед отправкой задачи
    
    def __init__(self, config: WorkerConfig):
        self.config = config
        self.session_id: str = f"bender-{self.WORKER_NAME}-{uuid.uuid4().hex[:8]}"
        self.status = WorkerStatus.IDLE
        self.current_task: Optional[str] = None
        self.start_time: Optional[float] = None
        self.log_buffer: List[str] = []
        self._process: Optional[asyncio.subprocess.Process] = None
        
    @property
    def effective_interval(self) -> float:
        """Интервал проверки с учётом множителя"""
        return self.config.check_interval * self.INTERVAL_MULTIPLIER
    
    @property
    @abstractmethod
    def cli_command(self) -> List[str]:
        """CLI команда для запуска (без задачи)"""
        pass
    
    @abstractmethod
    def format_task(self, task: str, context: Optional[str] = None) -> str:
        """Форматировать задачу для отправки в CLI"""
        pass
    
    def _get_tmux_session_cmd(self) -> List[str]:
        """Получить команду для запуска tmux сессии с CLI"""
        cli_cmd = self.cli_command
        # Правильно экранируем команду для shell
        cmd_str = shlex.join(cli_cmd)
        
        # Команда с cd в нужную директорию
        full_cmd = f"cd {shlex.quote(str(self.config.project_path))} && {cmd_str}"
        
        # Всегда запускаем detached, потом можем attach если нужно
        return [
            "tmux", "new-session", "-d", "-s", self.session_id,
            "bash", "-c", full_cmd
        ]
    
    async def start(self, task: str, context: Optional[str] = None) -> None:
        """Запустить worker с задачей"""
        self.current_task = task
        self.status = WorkerStatus.RUNNING
        self.start_time = time.time()
        self.log_buffer = []
        
        # Форматируем задачу (может обновить cli_command)
        formatted_task = self.format_task(task, context)
        logger.info(f"[{self.WORKER_NAME}] Starting: {task[:50]}...")
        
        # Получаем команду и запускаем tmux сессию
        cmd = self._get_tmux_session_cmd()
        try:
            cmd = [c for c in cmd if c]
            logger.debug(f"[{self.WORKER_NAME}] tmux command: {cmd}")
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.config.project_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await process.wait()
            logger.info(f"[{self.WORKER_NAME}] Session {self.session_id} started")
            
            # Открываем терминал если visible mode
            if self.config.visible:
                await self._open_terminal_window()
            
            # Ждём загрузки CLI и отправляем задачу
            await asyncio.sleep(self.STARTUP_DELAY)
            await self.send_input(formatted_task)
            logger.info(f"[{self.WORKER_NAME}] Task sent to CLI")
            
        except Exception as e:
            logger.error(f"[{self.WORKER_NAME}] Failed to start: {e}")
            self.status = WorkerStatus.ERROR
            raise
    
    async def _open_terminal_window(self) -> None:
        """Открыть новое окно терминала с tmux сессией"""
        import sys
        
        if sys.platform == "darwin":
            # macOS - открываем Terminal.app с tmux attach
            script = f'''
            tell application "Terminal"
                activate
                do script "tmux attach-session -t {self.session_id}"
            end tell
            '''
            try:
                proc = await asyncio.create_subprocess_exec(
                    "osascript", "-e", script,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await proc.wait()
                logger.info(f"[{self.WORKER_NAME}] Opened terminal window for session {self.session_id}")
            except Exception as e:
                logger.warning(f"[{self.WORKER_NAME}] Failed to open terminal: {e}")
        else:
            # Linux - пробуем разные терминалы
            terminals = [
                ["gnome-terminal", "--", "tmux", "attach-session", "-t", self.session_id],
                ["xterm", "-e", f"tmux attach-session -t {self.session_id}"],
                ["konsole", "-e", f"tmux attach-session -t {self.session_id}"],
            ]
            for term_cmd in terminals:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *term_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    # Не ждём завершения - терминал должен остаться открытым
                    logger.info(f"[{self.WORKER_NAME}] Opened terminal window for session {self.session_id}")
                    break
                except FileNotFoundError:
                    continue
            else:
                logger.warning(f"[{self.WORKER_NAME}] No terminal emulator found. Attach manually: tmux attach -t {self.session_id}")
    
    async def stop(self) -> None:
        """Остановить worker и закрыть терминал"""
        logger.info(f"[{self.WORKER_NAME}] Stopping session {self.session_id}")
        
        # Закрыть окно терминала если было открыто в visible mode
        if self.config.visible:
            await self._close_terminal_window()
        
        # Убить tmux сессию
        try:
            process = await asyncio.create_subprocess_exec(
                "tmux", "kill-session", "-t", self.session_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await process.wait()
        except Exception as e:
            logger.warning(f"[{self.WORKER_NAME}] Error stopping session: {e}")
        
        self.status = WorkerStatus.IDLE
        self.current_task = None
    
    async def _close_terminal_window(self) -> None:
        """Закрыть окно терминала с tmux сессией"""
        import sys
        
        if sys.platform == "darwin":
            # macOS - закрываем окно Terminal.app с нашей сессией
            script = f'''
            tell application "Terminal"
                set windowList to windows
                repeat with w in windowList
                    try
                        if name of w contains "{self.session_id}" then
                            close w
                        end if
                    end try
                end repeat
            end tell
            '''
            try:
                proc = await asyncio.create_subprocess_exec(
                    "osascript", "-e", script,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await proc.wait()
                logger.info(f"[{self.WORKER_NAME}] Closed terminal window for session {self.session_id}")
            except Exception as e:
                logger.warning(f"[{self.WORKER_NAME}] Failed to close terminal: {e}")
    
    async def capture_output(self) -> str:
        """Захватить текущий вывод из tmux сессии"""
        try:
            process = await asyncio.create_subprocess_exec(
                "tmux", "capture-pane", "-t", self.session_id, "-p", "-S", "-1000",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await process.communicate()
            output = stdout.decode("utf-8", errors="replace")
            return output
        except Exception as e:
            logger.warning(f"[{self.WORKER_NAME}] Error capturing output: {e}")
            return ""
    
    async def send_input(self, text: str) -> None:
        """Отправить ввод в tmux сессию"""
        try:
            process = await asyncio.create_subprocess_exec(
                "tmux", "send-keys", "-t", self.session_id, text, "Enter",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await process.wait()
        except Exception as e:
            logger.error(f"[{self.WORKER_NAME}] Error sending input: {e}")
    
    async def is_session_alive(self) -> bool:
        """Проверить, жива ли tmux сессия"""
        try:
            process = await asyncio.create_subprocess_exec(
                "tmux", "has-session", "-t", self.session_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            result = await process.wait()
            return result == 0
        except Exception:
            return False
    
    def get_elapsed_time(self) -> float:
        """Время с начала задачи"""
        if self.start_time is None:
            return 0.0
        return time.time() - self.start_time
    
    async def attach(self) -> None:
        """Присоединиться к tmux сессии (для --visible)"""
        subprocess.run(["tmux", "attach-session", "-t", self.session_id])
