"""
Interactive Copilot Worker - интерактивный режим с НАТИВНЫМ терминалом

В отличие от обычного CopilotWorker:
1. Открывает НАТИВНОЕ окно Terminal.app (не tmux внутри)
2. Запускает copilot напрямую - как будто ты сам набрал команду
3. Полный скролл, история, всё как обычно
4. Bender читает вывод через файл для автоответов

Преимущества:
- Терминал ТОЧНО такой же как когда работаешь сам
- Можно листать, скроллить, всё видно
- Если bender падает - терминал остаётся, продолжай вручную
"""

import asyncio
import logging
import os
import re
import signal
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Awaitable, List, Optional, Tuple
import uuid

from .base import BaseWorker, WorkerConfig, WorkerStatus
from ..log_watcher import LogWatcher
from ..log_filter import LogFilter
from ..console_recovery import ConsoleRecovery

logger = logging.getLogger(__name__)


@dataclass
class CopilotState:
    """Состояние интерактивной сессии copilot"""
    is_waiting_input: bool = False
    is_working: bool = False
    is_asking_permission: bool = False
    is_asking_question: bool = False
    last_question: str = ""
    permission_type: str = ""
    task_completed: bool = False
    completion_markers_found: List[str] = field(default_factory=list)


# Паттерны для детекции
PERMISSION_PATTERNS = [
    (r"Allow\s+(\w+)\s+for this session\?", "tool"),
    (r"(\w+)\s+wants to use", "tool"),
    (r"Allow tool:\s*(\w+)", "tool"),
    (r"Allow access to\s+(.+?)\?", "file"),
    (r"Allow writing to\s+(.+?)\?", "file"),
    (r"\[y/n\]", "yesno"),
    (r"\(y/N\)", "yesno"),
    (r"\(Y/n\)", "yesno"),
]


class InteractiveCopilotWorker(BaseWorker):
    """Интерактивный worker с НАТИВНЫМ терминалом
    
    Запускает copilot в обычном Terminal.app - точно так же как ты сам.
    Полный скролл, история, всё родное.
    """
    
    WORKER_NAME = "copilot-interactive"
    INTERVAL_MULTIPLIER = 1.0
    STARTUP_DELAY = 2.0
    
    def __init__(
        self,
        config: WorkerConfig,
        model: str = "claude-sonnet-4",
        on_status: Optional[Callable[[str], Awaitable[None]]] = None,
        on_question: Optional[Callable[[str], Awaitable[Optional[str]]]] = None,
        auto_allow_tools: bool = True,
        status_interval: float = 30.0,
        log_watcher: Optional[LogWatcher] = None,
    ):
        super().__init__(config)
        self.model = model
        self.on_status = on_status
        self.on_question = on_question
        self.auto_allow_tools = auto_allow_tools
        self.status_interval = status_interval
        self.log_watcher = log_watcher
        
        self._state = CopilotState()
        self._last_output = ""
        self._last_output_hash = ""
        self._last_status_time = 0.0
        self._monitor_task: Optional[asyncio.Task] = None
        self._task_start_time: Optional[float] = None
        self._current_task_text = ""
        
        # Для нативного терминала
        self._log_file: Optional[Path] = None
        self._terminal_pid: Optional[int] = None
        self._terminal_window_id: Optional[str] = None
        self._console_recovery = ConsoleRecovery()
    
    @property
    def cli_command(self) -> List[str]:
        return ["copilot", "--model", self.model]
    
    def format_task(self, task: str, context: Optional[str] = None) -> str:
        if context:
            return f"{task}\n\nКонтекст предыдущей работы:\n{context}"
        return task
    
    async def start(self, task: str, context: Optional[str] = None) -> None:
        """Запустить copilot в нативном Terminal.app"""
        self.current_task = task
        self._current_task_text = task
        self.status = WorkerStatus.RUNNING
        self.start_time = time.time()
        self._task_start_time = time.time()
        self._state = CopilotState()
        self._console_recovery.reset()
        
        formatted_task = self.format_task(task, context)
        logger.info(f"[{self.WORKER_NAME}] Starting native terminal: {task[:50]}...")
        
        # Создаём лог-файл для чтения вывода
        self._log_file = Path(tempfile.gettempdir()) / f"bender-{self.session_id}.log"
        
        # Открываем нативный терминал с copilot
        await self._open_native_terminal(formatted_task)
        await asyncio.sleep(self.STARTUP_DELAY)
        await self._send_task_to_terminal(formatted_task)
        
        # Запускаем мониторинг лог-файла
        if not self._monitor_task or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._monitor_loop())
    
    async def _open_native_terminal(self, task: str) -> None:
        """Открыть Terminal.app и запустить copilot с задачей"""
        import shlex
        
        # Пишем задачу в файл чтобы избежать проблем с экранированием
        task_file = Path(tempfile.gettempdir()) / f"bender-task-{self.session_id}.txt"
        task_file.write_text(task)
        
        # Создаём shell-скрипт для запуска
        script_file = Path(tempfile.gettempdir()) / f"bender-run-{self.session_id}.sh"
        script_content = f'''#!/bin/bash
cd {shlex.quote(str(self.config.project_path))}
script -q {shlex.quote(str(self._log_file))} copilot --model {shlex.quote(self.model)} --allow-all
'''
        script_file.write_text(script_content)
        script_file.chmod(0o755)
        
        # AppleScript для открытия Terminal.app (нормальный размер окна)
        applescript = f'''
        tell application "Terminal"
            do script "{script_file}"
            delay 0.3
            set windowId to id of front window
            tell front window
                set zoomed to false
                set bounds to {{100, 100, 1000, 700}}
            end tell
            return windowId
        end tell
        '''
        
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", applescript,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            logger.info(f"[{self.WORKER_NAME}] Native terminal opened")
            if stdout:
                self._terminal_window_id = stdout.decode().strip()
            
        except Exception as e:
            logger.error(f"[{self.WORKER_NAME}] Failed to open terminal: {e}")
            self.status = WorkerStatus.ERROR
            raise

    async def send_input(self, text: str) -> None:
        """Отправить текст в нативный Terminal.app"""
        # Используем базовый метод, но пытаемся отправить в Terminal даже если visible=False
        sent = await self._send_text_to_terminal(text)
        if sent:
            return
        await super().send_input(text)

    def _prepare_task_for_input(self, task: str) -> str:
        """Свести многострочную задачу к одной строке для интерактивного ввода"""
        compact = " ".join(line.strip() for line in task.splitlines() if line.strip())
        # Сжать лишние пробелы
        return " ".join(compact.split())

    async def _send_task_to_terminal(self, task: str) -> None:
        """Отправить задачу в интерактивный copilot"""
        task_line = self._prepare_task_for_input(task)
        await self.send_input(task_line)
        if self.on_status:
            await self.on_status("📤 Task sent to copilot")
    
    async def _send_keystroke(self, key: str) -> None:
        """Отправить нажатие клавиши в Terminal.app"""
        window_id = getattr(self, "_terminal_window_id", None)
        window_select = ""
        if window_id:
            window_select = f"""
                try
                    set front window to (first window whose id is {window_id})
                end try
            """
        applescript = f'''
        tell application "Terminal"
            activate
            {window_select}
        end tell
        tell application "System Events"
            keystroke "{key}"
        end tell
        '''
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", applescript,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await proc.wait()
        except Exception as e:
            logger.warning(f"[{self.WORKER_NAME}] Keystroke failed: {e}")
    
    async def capture_output(self, lines: int = 200) -> str:
        """Читать вывод из лог-файла"""
        if not self._log_file or not self._log_file.exists():
            return ""
        try:
            content = self._log_file.read_text(errors='replace')
            # Берём последние N строк
            all_lines = content.split('\n')
            return '\n'.join(all_lines[-lines:])
        except Exception as e:
            logger.warning(f"[{self.WORKER_NAME}] Error reading log: {e}")
            return ""
    
    async def capture_full_scrollback(self) -> str:
        """Получить весь вывод"""
        if not self._log_file or not self._log_file.exists():
            return ""
        try:
            return self._log_file.read_text(errors='replace')
        except Exception:
            return ""
    
    def _detect_state(self, output: str) -> CopilotState:
        """Определить состояние copilot
        
        Проверяем только permission requests — завершение определяет LLM.
        """
        state = CopilotState()
        recent_lines = output.strip().split('\n')[-30:]
        recent_text = '\n'.join(recent_lines)
        
        # Проверяем разрешения
        for pattern, perm_type in PERMISSION_PATTERNS:
            if re.search(pattern, recent_text, re.IGNORECASE):
                state.is_asking_permission = True
                state.permission_type = perm_type
                return state
        
        state.is_working = True
        return state
    
    async def _handle_permission(self, state: CopilotState) -> None:
        """Авто-ответить на запрос разрешения"""
        if not state.is_asking_permission:
            return
        
        if self.auto_allow_tools:
            logger.info(f"[{self.WORKER_NAME}] Auto-allowing {state.permission_type}")
            await self._send_keystroke("y")
            
            if self.on_status:
                await self.on_status(f"✅ Auto-allowed: {state.permission_type}")
    
    async def _report_status(self, output: str) -> None:
        """Сообщить статус — человеко-читаемый через LogWatcher если есть"""
        now = time.time()
        if now - self._last_status_time < self.status_interval:
            return
        
        self._last_status_time = now
        elapsed = int(now - (self._task_start_time or now))
        
        # Пробуем через LogWatcher для человеко-читаемого статуса
        if self.log_watcher and len(output) > 100:
            try:
                analysis = await self.log_watcher.analyze(
                    output, 
                    self._current_task_text, 
                    float(elapsed)
                )
                status_msg = f"⏳ [{elapsed}s] {analysis.summary[:60]}"
                if self.on_status:
                    await self.on_status(status_msg)
                logger.info(f"[{self.WORKER_NAME}] Status: {status_msg}")
                return
            except Exception as e:
                logger.debug(f"LogWatcher failed, using fallback: {e}")
        
        # Fallback: последняя значимая строка лога
        # Полная очистка ANSI/terminal escape sequences
        clean_output = re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]', '', output)  # CSI sequences
        clean_output = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?', '', clean_output)  # OSC sequences
        clean_output = re.sub(r'\x1b[=>]', '', clean_output)  # Mode switches
        clean_output = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', clean_output)  # Control chars
        
        lines = [l.strip() for l in clean_output.split('\n') if l.strip() and len(l.strip()) > 5]
        last_meaningful = lines[-1][:80] if lines else "working..."
        
        status_msg = f"⏳ [{elapsed}s] {last_meaningful}"
        
        if self.on_status:
            await self.on_status(status_msg)
        logger.info(f"[{self.WORKER_NAME}] Status: {status_msg}")
    
    async def _monitor_loop(self) -> None:
        """Мониторинг лог-файла — обработка permissions"""
        check_interval = 2.0
        
        while True:
            try:
                await asyncio.sleep(check_interval)
                
                output = await self.capture_output(lines=100)
                
                output_hash = hash(output[-500:] if len(output) > 500 else output)
                if output_hash == self._last_output_hash:
                    continue
                
                self._last_output_hash = output_hash
                self._last_output = output
                
                self._state = self._detect_state(output)
                
                # Обработка разрешений
                if self._state.is_asking_permission:
                    await self._handle_permission(self._state)
                    continue

                # Если консоль упала — мягко подтолкнуть
                console_issue = self._console_recovery.detect_issue(output)
                if console_issue:
                    recovered = await self._console_recovery.attempt_recovery(
                        worker=self,
                        on_status=self.on_status,
                        reason=console_issue,
                        output=output,
                    )
                    if recovered:
                        continue
                
            except asyncio.CancelledError:
                logger.info(f"[{self.WORKER_NAME}] Monitor cancelled")
                break
            except Exception as e:
                logger.error(f"[{self.WORKER_NAME}] Monitor error: {e}")
                await asyncio.sleep(5)
    
    async def wait_for_completion(self, timeout: float = 1800) -> Tuple[bool, str]:
        """Дождаться завершения — LLM решает когда готово"""
        start = time.time()
        check_interval = 30  # LLM проверка каждые 30 секунд
        
        while time.time() - start < timeout:
            await asyncio.sleep(check_interval)
            elapsed = time.time() - start
            
            # Проверяем жив ли терминал
            if not await self.is_session_alive():
                logger.warning(f"[{self.WORKER_NAME}] Terminal died after {int(elapsed)}s")
                self.status = WorkerStatus.ERROR
                return False, await self.capture_full_scrollback()
            
            # Если status уже установлен монитором
            if self.status == WorkerStatus.COMPLETED:
                return True, await self.capture_full_scrollback()
            
            if self.status == WorkerStatus.ERROR:
                return False, await self.capture_full_scrollback()
            
            # LLM анализ
            output = await self.capture_full_scrollback()
            if self.log_watcher and len(output) > 100:
                try:
                    analysis = await self.log_watcher.analyze(
                        output[-8000:],
                        self._current_task_text,
                        elapsed
                    )
                    
                    # Репорт статуса
                    if self.on_status:
                        await self.on_status(f"⏳ [{int(elapsed)}s] {analysis.summary[:60]}")
                    
                    if analysis.result.value == "completed":
                        self.status = WorkerStatus.COMPLETED
                        logger.info(f"[{self.WORKER_NAME}] LLM says completed: {analysis.summary}")
                        return True, output
                    
                    if analysis.result.value == "error":
                        self.status = WorkerStatus.ERROR
                        logger.warning(f"[{self.WORKER_NAME}] LLM detected error")
                        return False, output
                        
                except Exception as e:
                    logger.debug(f"LLM analyze failed: {e}")
        
        logger.warning(f"[{self.WORKER_NAME}] Timeout after {timeout}s")
        self.status = WorkerStatus.STUCK
        return False, await self.capture_full_scrollback()
    
    async def send_next_task(self, task: str, context: Optional[str] = None) -> None:
        """Отправить следующую задачу (переиспользуем текущий терминал)"""
        # Если сессия не жива — создаём новую
        if not await self.is_session_alive():
            await self.start(task, context)
            return
        
        self.current_task = task
        self._current_task_text = task
        self.status = WorkerStatus.RUNNING
        self.start_time = time.time()
        self._task_start_time = time.time()
        self._state = CopilotState()
        self._console_recovery.reset()
        self._last_output_hash = ""
        self._last_output = ""
        
        formatted_task = self.format_task(task, context)
        await self._send_task_to_terminal(formatted_task)
        
        if not self._monitor_task or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._monitor_loop())
    
    async def stop(self) -> None:
        """Остановить worker и закрыть терминал"""
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        
        # Закрыть окно Terminal
        await self._close_terminal()
        logger.info(f"[{self.WORKER_NAME}] Stopped and closed terminal.")
        
        self.status = WorkerStatus.IDLE
        self.current_task = None
    
    async def stop_keep_terminal(self) -> None:
        """Остановить worker но оставить терминал открытым"""
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        
        logger.info(f"[{self.WORKER_NAME}] Stopped. Terminal left open for manual work.")
        if self.on_status:
            await self.on_status("💡 Terminal left open - continue manually if needed")
        
        self.status = WorkerStatus.IDLE
        self.current_task = None
    
    async def _close_terminal(self) -> None:
        """Закрыть окно терминала"""
        window_id = getattr(self, "_terminal_window_id", None)
        if window_id:
            applescript = f'''
            tell application "Terminal"
                try
                    close (first window whose id is {window_id}) saving no
                end try
            end tell
            '''
        else:
            applescript = '''
            tell application "Terminal"
                close front window
            end tell
            '''
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", applescript,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await proc.wait()
        except Exception as e:
            logger.warning(f"[{self.WORKER_NAME}] Failed to close terminal: {e}")
        
        # Удалить временные файлы
        self._cleanup_temp_files()
    
    async def force_stop(self) -> None:
        """Принудительно остановить и закрыть терминал"""
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        
        await self._close_terminal()
        self.status = WorkerStatus.IDLE
    
    def _cleanup_temp_files(self) -> None:
        """Удалить временные файлы"""
        import tempfile
        
        # Удалить лог-файл
        if self._log_file and self._log_file.exists():
            try:
                self._log_file.unlink()
            except Exception:
                pass
        
        # Удалить файл задачи
        task_file = Path(tempfile.gettempdir()) / f"bender-task-{self.session_id}.txt"
        if task_file.exists():
            try:
                task_file.unlink()
            except Exception:
                pass
        
        # Удалить скрипт
        script_file = Path(tempfile.gettempdir()) / f"bender-run-{self.session_id}.sh"
        if script_file.exists():
            try:
                script_file.unlink()
            except Exception:
                pass
    
    async def is_session_alive(self) -> bool:
        """Проверить живой ли терминал"""
        # 1. Проверяем существует ли окно терминала
        if self._terminal_window_id:
            try:
                check_script = f'''
                tell application "Terminal"
                    try
                        set w to first window whose id is {self._terminal_window_id}
                        return "alive"
                    on error
                        return "dead"
                    end try
                end tell
                '''
                proc = await asyncio.subprocess.create_subprocess_exec(
                    "osascript", "-e", check_script,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
                if "dead" in stdout.decode():
                    logger.warning(f"[{self.WORKER_NAME}] Terminal window {self._terminal_window_id} is dead")
                    return False
            except asyncio.TimeoutError:
                pass  # Terminal app might be slow, continue with log check
            except Exception as e:
                logger.debug(f"Terminal window check failed: {e}")
        
        # 2. Проверяем обновляется ли лог-файл
        if not self._log_file or not self._log_file.exists():
            return False
        try:
            mtime = self._log_file.stat().st_mtime
            return (time.time() - mtime) < 120  # Обновлялся последние 2 минуты
        except Exception:
            return False
    
    def get_state(self) -> CopilotState:
        return self._state
    
    def get_last_output(self) -> str:
        return self._last_output
