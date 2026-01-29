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
    TIMEOUT = "timeout"     # Таймаут


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
    check_interval: float = 60.0  # Как часто проверять логи
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
        self._monitor_task: Optional[asyncio.Task] = None
        self._log_file: Optional[Path] = None
        
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
    
    def _get_tmux_session_cmd(self, task: Optional[str] = None) -> List[str]:
        """Получить команду для запуска tmux сессии с CLI (для background режима)
        
        Args:
            task: Задача для передачи в команду (для droid exec режима)
        """
        cli_cmd = self.cli_command
        cmd_str = shlex.join(cli_cmd)
        
        # Для droid exec задачу нужно передать как аргумент
        if self.WORKER_NAME == "droid" and task:
            # Экранируем задачу для shell
            escaped_task = task.replace("'", "'\"'\"'")
            full_cmd = f"cd {shlex.quote(str(self.config.project_path))} && {cmd_str} $'{escaped_task}'"
        else:
            full_cmd = f"cd {shlex.quote(str(self.config.project_path))} && {cmd_str}"
        
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
        
        formatted_task = self.format_task(task, context)
        logger.info(f"[{self.WORKER_NAME}] Starting: {task[:50]}...")
        
        if self.config.visible:
            # Visible mode: нативный Terminal.app (без tmux!)
            await self._start_native_terminal(formatted_task)
        else:
            # Background mode: tmux
            await self._start_tmux_session(formatted_task)
    
    async def _start_tmux_session(self, task: str) -> None:
        """Запустить в tmux (background режим)"""
        # Для droid передаём задачу в команду, для остальных — через send_input
        if self.WORKER_NAME == "droid":
            cmd = self._get_tmux_session_cmd(task)
        else:
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
            
            # Для droid задача уже передана в команду
            if self.WORKER_NAME != "droid":
                await asyncio.sleep(self.STARTUP_DELAY)
                await self.send_input(task)
                logger.info(f"[{self.WORKER_NAME}] Task sent to CLI")
            
        except Exception as e:
            logger.error(f"[{self.WORKER_NAME}] Failed to start: {e}")
            self.status = WorkerStatus.ERROR
            raise
    
    async def _start_native_terminal(self, task: str) -> None:
        """Запустить в нативном Terminal.app (visible режим)"""
        import tempfile
        from pathlib import Path
        
        # Создаём лог-файл (session_id уже содержит "bender-")
        self._log_file = Path(tempfile.gettempdir()) / f"{self.session_id}.log"
        
        # Пишем задачу в файл
        task_file = Path(tempfile.gettempdir()) / f"bender-task-{self.session_id}.txt"
        task_file.write_text(task)
        
        # Создаём shell-скрипт - команда зависит от worker'а
        cli_cmd = shlex.join(self.cli_command)
        script_file = Path(tempfile.gettempdir()) / f"bender-run-{self.session_id}.sh"
        
        # Для copilot используем -p, для droid зависит от режима
        if self.WORKER_NAME in ("copilot", "copilot-interactive"):
            # copilot -p "task"
            cmd_with_task = f'{cli_cmd} -p "$(cat {shlex.quote(str(task_file))})"'
            # script для записи TTY вывода
            script_content = f'''#!/bin/bash
cd {shlex.quote(str(self.config.project_path))}
script -q {shlex.quote(str(self._log_file))} {cmd_with_task}
'''
        elif self.WORKER_NAME == "droid":
            if self.config.visible:
                # Visible: интерактивный droid с TUI (как copilot)
                cmd_with_task = f'{cli_cmd} "$(cat {shlex.quote(str(task_file))})"'
                script_content = f'''#!/bin/bash
cd {shlex.quote(str(self.config.project_path))}
script -q {shlex.quote(str(self._log_file))} {cmd_with_task}
'''
            else:
                # Background: droid exec -f (без TUI, чистый вывод)
                cmd_with_task = f'{cli_cmd} -f {shlex.quote(str(task_file))}'
                script_content = f'''#!/bin/bash
cd {shlex.quote(str(self.config.project_path))}
{cmd_with_task} 2>&1 | tee {shlex.quote(str(self._log_file))}
'''
        else:
            # codex и другие: просто передаём как аргумент
            cmd_with_task = f'{cli_cmd} "$(cat {shlex.quote(str(task_file))})"'
            script_content = f'''#!/bin/bash
cd {shlex.quote(str(self.config.project_path))}
script -q {shlex.quote(str(self._log_file))} {cmd_with_task}
'''
        script_file.write_text(script_content)
        script_file.chmod(0o755)
        
        # AppleScript - открываем Terminal.app, сохраняем ID окна
        self._terminal_window_id = None
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
            stdout, stderr = await proc.communicate()
            if stdout:
                self._terminal_window_id = stdout.decode().strip()
                logger.info(f"[{self.WORKER_NAME}] Native terminal opened, window ID: {self._terminal_window_id}")
            else:
                logger.info(f"[{self.WORKER_NAME}] Native terminal opened")
            
            # Ждём пока процесс реально запустится и создаст лог
            await asyncio.sleep(3.0)  # Даём время на запуск
            
            # Запускаем мониторинг лог-файла
            self._monitor_task = asyncio.create_task(self._monitor_native_terminal())
            
        except Exception as e:
            logger.error(f"[{self.WORKER_NAME}] Failed to open terminal: {e}")
            self.status = WorkerStatus.ERROR
            raise
    
    async def _monitor_native_terminal(self) -> None:
        """Мониторинг нативного терминала"""
        check_interval = 2.0
        last_hash = ""
        
        completion_markers = [
            "Total usage est:",
            "Total session time:",
            "Breakdown by AI model:",
        ]
        
        while True:
            try:
                await asyncio.sleep(check_interval)
                
                if self._log_file is None or not self._log_file.exists():
                    continue
                
                content = self._log_file.read_text(errors='replace')
                content_hash = hash(content[-500:] if len(content) > 500 else content)
                
                if content_hash == last_hash:
                    continue
                last_hash = content_hash
                
                # Проверяем завершение
                for marker in completion_markers:
                    if marker in content:
                        logger.info(f"[{self.WORKER_NAME}] Task completed!")
                        self.status = WorkerStatus.COMPLETED
                        return
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[{self.WORKER_NAME}] Monitor error: {e}")
                await asyncio.sleep(5)
    
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
        
        # Остановить мониторинг если есть
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        
        if self.config.visible:
            # Visible mode: закрыть нативный терминал
            await self._close_native_terminal()
        else:
            # Background mode: убить tmux сессию
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
    
    async def _close_native_terminal(self) -> None:
        """Закрыть нативное окно терминала"""
        import sys
        import tempfile
        
        if sys.platform == "darwin":
            window_id = getattr(self, '_terminal_window_id', None)
            
            # Сначала убиваем процесс script если он ещё работает
            try:
                find_proc = await asyncio.create_subprocess_shell(
                    f"pgrep -f 'script.*{self.session_id}'",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL
                )
                stdout, _ = await find_proc.communicate()
                if stdout:
                    pids = stdout.decode().strip().split('\n')
                    for pid in pids:
                        if pid.isdigit():
                            kill_proc = await asyncio.create_subprocess_exec(
                                "kill", "-9", pid,
                                stdout=asyncio.subprocess.DEVNULL,
                                stderr=asyncio.subprocess.DEVNULL
                            )
                            await kill_proc.wait()
            except Exception:
                pass
            
            await asyncio.sleep(0.3)
            
            # Закрываем ТОЛЬКО по сохранённому window_id - чтобы не закрыть чужие окна!
            if window_id:
                # Пробуем close по ID
                script = f'''
                tell application "Terminal"
                    try
                        close (first window whose id is {window_id}) saving no
                    end try
                end tell
                '''
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "osascript", "-e", script,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL
                    )
                    await proc.wait()
                    logger.info(f"[{self.WORKER_NAME}] Closed terminal window {window_id}")
                except Exception as e:
                    logger.warning(f"[{self.WORKER_NAME}] Failed to close window {window_id}: {e}")
            else:
                logger.warning(f"[{self.WORKER_NAME}] No window_id saved, cannot close terminal safely")
        
        # Удалить временные файлы
        for pattern in ["bender-task-", "bender-run-", "bender-winid-", "bender-"]:
            temp_file = Path(tempfile.gettempdir()) / f"{pattern}{self.session_id}"
            for suffix in ["", ".txt", ".sh", ".log"]:
                f = Path(str(temp_file) + suffix)
                if f.exists():
                    try:
                        f.unlink()
                    except Exception:
                        pass
    
    async def capture_output(self) -> str:
        """Захватить текущий вывод (из лог-файла или tmux)"""
        # Visible mode: читаем из лог-файла
        if self._log_file is not None and self._log_file.exists():
            try:
                return self._log_file.read_text(errors='replace')
            except Exception:
                pass
        
        # Fallback: tmux (для невидимого режима)
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

    async def _send_text_to_terminal(self, text: str) -> bool:
        """Отправить текст в нативный Terminal.app (macOS)"""
        import sys
        import json

        if sys.platform != "darwin":
            return False

        window_id = getattr(self, "_terminal_window_id", None)
        text_payload = json.dumps(text or "")

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
            if {text_payload} is not "" then
                keystroke {text_payload}
            end if
            key code 36
        end tell
        '''
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", applescript,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await proc.wait()
            return True
        except Exception as e:
            logger.warning(f"[{self.WORKER_NAME}] Native terminal input failed: {e}")
            return False

    async def send_input(self, text: str) -> None:
        """Отправить ввод в tmux сессию"""
        # Visible mode: попытаться отправить в нативный терминал (macOS)
        if self.config.visible:
            sent = await self._send_text_to_terminal(text)
            if sent:
                return

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
        """Проверить, жива ли сессия (tmux или native terminal)"""
        if self.config.visible:
            # Visible mode: проверяем что процесс script ещё работает
            try:
                import subprocess
                # Ищем процесс по session_id (в имени скрипта)
                result = subprocess.run(
                    ["pgrep", "-f", self.session_id],
                    capture_output=True,
                    text=True
                )
                return result.returncode == 0
            except Exception:
                return False
        else:
            # Background mode: проверяем tmux сессию
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
