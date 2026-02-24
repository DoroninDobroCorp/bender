"""
Base Worker - абстрактный класс для CLI workers

FIXES:
- Async file I/O (aiofiles) - не блокирует event loop
- Atexit cleanup для temp files - предотвращает disk leaks
- Subprocess timeouts - защита от зависаний
- Proper exception logging - не скрывает ошибки
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, List, Callable, Awaitable


if TYPE_CHECKING:
    from uuid import UUID
    from backend.core.events.schema import EventType, UniversalEvent

logger = logging.getLogger(__name__)


class GenericStreamingAdapter:
    """Generic streaming adapter для plain text workers (copilot, codex).

    Конвертирует plain text output в UniversalEvent последовательность:
    - SESSION_START
    - ITEM_START + ITEM_DELTA (по мере поступления текста)
    - ITEM_END + SESSION_END

    Usage:
        adapter = GenericStreamingAdapter(agent_type="copilot")
        session_event = adapter.start_session(agent_id="copilot-w1")
        delta_events = adapter.feed_text("chunk of text")
        end_events = adapter.end_session()
    """

    def __init__(self, agent_type: str) -> None:
        self._agent_type = agent_type
        self._sequence = 0
        self._agent_id: Optional[str] = None
        self._session_id: Optional[str] = None
        self._user_id: Optional[uuid.UUID] = None
        self._item_open = False

    @property
    def agent_type(self) -> str:
        return self._agent_type

    def _next_seq(self) -> int:
        seq = self._sequence
        self._sequence += 1
        return seq

    def _make_event(
        self,
        event_type: EventType,
        data: Optional[dict] = None
    ) -> UniversalEvent:
        from backend.core.events.schema import EventSource, UniversalEvent

        if self._agent_id is None:
            raise RuntimeError("Session not started. Call start_session() first.")

        return UniversalEvent(
            event_type=event_type,
            sequence=self._next_seq(),
            agent_id=self._agent_id,
            agent_type=self.agent_type,
            session_id=self._session_id,
            user_id=self._user_id,
            source=EventSource.DAEMON,
            synthetic=True,
            data=data or {},
        )

    def start_session(
        self,
        *,
        agent_id: str,
        session_id: Optional[str] = None,
        user_id: Optional[uuid.UUID] = None,
    ) -> UniversalEvent:
        """Start synthetic session → SESSION_START event."""
        from backend.core.events.schema import EventType

        self._agent_id = agent_id
        self._session_id = session_id
        self._user_id = user_id
        self._item_open = False
        self._sequence = 0
        return self._make_event(EventType.SESSION_START)

    # 10 MB limit per chunk to prevent DoS via large text
    MAX_TEXT_CHUNK_SIZE = 10 * 1024 * 1024

    def feed_text(self, text: str) -> list[UniversalEvent]:
        """Feed text chunk → ITEM_START (first call) + ITEM_DELTA."""
        from backend.core.events.schema import EventType

        if self._agent_id is None:
            raise RuntimeError("Session not started. Call start_session() first.")

        # Early exit for empty/whitespace-only text (before truncation — intentional order)
        if not text or not text.strip():
            return []

        # Truncate oversized chunks to prevent memory issues
        if len(text) > self.MAX_TEXT_CHUNK_SIZE:
            text = text[:self.MAX_TEXT_CHUNK_SIZE]

        events = []

        if not self._item_open:
            events.append(self._make_event(EventType.ITEM_START))
            self._item_open = True

        events.append(self._make_event(EventType.ITEM_DELTA, {"text": text}))
        return events

    def end_session(self) -> list[UniversalEvent]:
        """End session → ITEM_END (if open) + SESSION_END."""
        from backend.core.events.schema import EventType

        if self._agent_id is None:
            raise RuntimeError("Session not started. Call start_session() first.")

        events = []

        if self._item_open:
            events.append(self._make_event(EventType.ITEM_END))
            self._item_open = False

        events.append(self._make_event(EventType.SESSION_END))

        self._agent_id = None
        return events


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
    events: list = field(default_factory=list)  # List[UniversalEvent]


@dataclass
class WorkerConfig:
    """Конфигурация worker'а"""
    project_path: Path
    check_interval: float = 60.0  # Как часто проверять логи
    visible: bool = False         # Показывать терминал
    simple_mode: bool = False     # Без перепроверки
    max_retries: int = 3          # Максимум перезапусков
    stuck_timeout: float = 300.0  # Inactivity timeout: убить если вывод не менялся N сек
    max_total_timeout: float = 28800.0  # Макс общее время работы (8 часов)
    ssh_host: str | None = None   # SSH host для удалённого запуска


class BaseWorker(ABC):
    """Базовый класс для CLI workers
    
    Workers запускают CLI инструменты (copilot, droid, codex) в tmux сессиях
    и следят за их выполнением.
    """
    
    WORKER_NAME: str = "base"
    INTERVAL_MULTIPLIER: float = 1.0  # Для codex = 2.0
    
    STARTUP_DELAY: float = 2.0  # Время на загрузку CLI перед отправкой задачи
    
    # Время после которого orphan терминал считается "мёртвым" (секунды)
    ORPHAN_TERMINAL_AGE: float = 7200.0  # 2 часа
    
    # Паттерны завершения работы (переопределяются в наследниках)
    COMPLETION_PATTERNS: List[str] = [
        "Task completed",
        "All done",
        "Successfully",
        "Готово",
        "Завершено",
    ]
    
    # Паттерны shell prompt (возврат в shell = завершение)
    SHELL_PROMPT_PATTERNS: List[str] = [
        r"\$ $",           # bash prompt
        r"% $",            # zsh prompt
        r"> $",            # generic prompt
        r"vladimirdoronin@",  # user-specific
    ]
    
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
        self._done_file: Optional[Path] = None
        self._last_output_len: int = 0
        self._no_change_count: int = 0
        self._spawned_pids: set[int] = set()  # PIDs процессов которые МЫ запустили
        self._terminal_window_id: Optional[str] = None  # ID окна терминала
        self._terminal_tab_id: Optional[str] = None  # ID вкладки терминала (macOS)
        self._terminal_tty: Optional[str] = None  # TTY вкладки терминала (macOS)
        self._terminal_tty_file: Optional[Path] = None  # Файл с tty для fallback
        self._completed: bool = False  # Флаг завершения (для event_stream)
        self._output: str = ""  # Захваченный output (для event_stream)
        
        # FIX #2: Регистрируем cleanup temp files при exit
        # Это гарантирует удаление даже при crash
        atexit.register(self._cleanup_temp_files_sync)
    
    def _cleanup_temp_files_sync(self):
        """Синхронный cleanup для atexit (вызывается при exit программы)
        
        FIX #2: Гарантированное удаление temp files даже при crash
        """
        try:
            if self._log_file and self._log_file.exists():
                self._log_file.unlink()
            if self._done_file and self._done_file.exists():
                self._done_file.unlink()
            if self._terminal_tty_file and self._terminal_tty_file.exists():
                self._terminal_tty_file.unlink()
        except Exception:
            pass  # Ignore errors during cleanup
    
    @classmethod
    async def kill_orphan_terminals(cls) -> int:
        """Убить все orphan bender-терминалы и tmux сессии
        
        Вызывается при старте нового worker'а для предотвращения накопления терминалов.
        
        Returns:
            Количество убитых терминалов/сессий
        """
        killed_count = 0
        
        # 1. Убиваем все bender-* tmux сессии
        try:
            # Получаем список bender-* сессий
            proc = await asyncio.create_subprocess_exec(
                "tmux", "list-sessions", "-F", "#{session_name}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            stdout, _ = await proc.communicate()
            
            if stdout:
                for session in stdout.decode().strip().split('\n'):
                    if session.startswith('bender-'):
                        try:
                            kill_proc = await asyncio.create_subprocess_exec(
                                "tmux", "kill-session", "-t", session,
                                stdout=asyncio.subprocess.DEVNULL,
                                stderr=asyncio.subprocess.DEVNULL
                            )
                            await kill_proc.wait()
                            killed_count += 1
                            logger.info(f"[cleanup] Killed orphan tmux session: {session}")
                        except Exception:
                            pass
        except Exception as e:
            logger.debug(f"[cleanup] Error listing tmux sessions: {e}")
        
        # 2. Убиваем все процессы с bender- в командной строке
        try:
            proc = await asyncio.create_subprocess_exec(
                "pgrep", "-f", "bender-",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            stdout, _ = await proc.communicate()
            
            if stdout:
                pids = [p.strip() for p in stdout.decode().strip().split('\n') if p.strip().isdigit()]
                for pid in pids:
                    try:
                        kill_proc = await asyncio.create_subprocess_exec(
                            "kill", "-15", pid,
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL
                        )
                        await kill_proc.wait()
                        killed_count += 1
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"[cleanup] Error killing bender processes: {e}")
        
        # 3. Удаляем старые temp файлы (log, done, task, run, inner)
        try:
            temp_dir = Path(tempfile.gettempdir())
            for pattern in ["bender-*.log", "bender-*.done", "bender-task-*", "bender-run-*", "bender-inner-*", "bender-tty-*"]:
                for f in temp_dir.glob(pattern):
                    try:
                        # Удаляем файлы старше 2 часов
                        if time.time() - f.stat().st_mtime > cls.ORPHAN_TERMINAL_AGE:
                            f.unlink()
                            logger.debug(f"[cleanup] Deleted old temp file: {f.name}")
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"[cleanup] Error cleaning temp files: {e}")
        
        if killed_count > 0:
            logger.warning(f"[cleanup] Killed {killed_count} orphan terminals/processes")
        
        return killed_count
    
    @classmethod
    async def count_active_terminals(cls) -> int:
        """Подсчитать количество активных bender-терминалов
        
        Returns:
            Количество активных терминалов
        """
        count = 0
        
        # 1. Считаем tmux сессии
        try:
            proc = await asyncio.create_subprocess_exec(
                "tmux", "list-sessions", "-F", "#{session_name}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            stdout, _ = await proc.communicate()
            
            if stdout:
                for session in stdout.decode().strip().split('\n'):
                    if session.startswith('bender-'):
                        count += 1
        except Exception:
            pass
        
        return count
    
    def detect_completion(self, output: str) -> Optional[str]:
        """Детектировать завершение по паттернам в логе
        
        Returns:
            Причина завершения или None если не завершено
        """
        # Проверяем последние 3000 символов
        last_chunk = output[-3000:] if len(output) > 3000 else output
        
        # Проверяем паттерны завершения
        for pattern in self.COMPLETION_PATTERNS:
            if pattern in last_chunk:
                return f"completion pattern: {pattern}"
        
        # Проверяем shell prompt в конце (последние 200 символов)
        last_lines = output[-200:] if len(output) > 200 else output
        for pattern in self.SHELL_PROMPT_PATTERNS:
            if re.search(pattern, last_lines):
                return f"shell prompt detected"
        
        return None
    
    def detect_stuck(self, output: str) -> bool:
        """Детектировать зависание (лог не меняется)
        
        Returns:
            True если зависло (нет изменений 10 раз подряд = ~5 минут)
        """
        current_len = len(output)
        if current_len == self._last_output_len:
            self._no_change_count += 1
            if self._no_change_count >= 10:  # 10 * 30s = 300s = 5 минут
                return True
        else:
            self._no_change_count = 0
            self._last_output_len = current_len
        return False
        
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

    @abstractmethod
    def _create_streaming_adapter(self) -> GenericStreamingAdapter:
        """Создать StreamingAdapter для конвертации output → UniversalEvent.

        Каждый worker возвращает свой адаптер:
        - DroidWorker → DroidAdapter (наследует GenericStreamingAdapter protocol)
        - CopilotWorker → GenericStreamingAdapter
        - CodexWorker → GenericStreamingAdapter

        Returns:
            Адаптер с методами: start_session(), feed_text(), end_session()
        """
        pass
    
    def _get_tmux_session_cmd(self, task: Optional[str] = None) -> List[str]:
        """Получить команду для запуска tmux сессии с CLI (для background режима)
        
        Args:
            task: Задача для передачи в команду (для droid/codex exec режима)
        """
        cli_cmd = self.cli_command
        cmd_str = shlex.join(cli_cmd)
        
        # Создаём лог файл для захвата вывода
        self._log_file = Path(tempfile.gettempdir()) / f"{self.session_id}.log"
        self._done_file = Path(tempfile.gettempdir()) / f"{self.session_id}.done"
        log_path = shlex.quote(str(self._log_file))
        done_path = shlex.quote(str(self._done_file))
        
        # Удаляем старые файлы
        if self._log_file.exists():
            self._log_file.unlink()
        if self._done_file.exists():
            self._done_file.unlink()
        
        # Для droid/codex exec задачу нужно передать как аргумент
        if self.WORKER_NAME in ("droid", "codex") and task:
            # Экранируем задачу для shell
            escaped_task = task.replace("'", "'\"'\"'")
            # ВАЖНО: пишем вывод в лог файл через tee, exit code в .done
            full_cmd = f"cd {shlex.quote(str(self.config.project_path))} && {cmd_str} $'{escaped_task}' 2>&1 | tee {log_path}; echo ${{PIPESTATUS[0]}} > {done_path}"
        else:
            full_cmd = f"cd {shlex.quote(str(self.config.project_path))} && {cmd_str} 2>&1 | tee {log_path}; echo ${{PIPESTATUS[0]}} > {done_path}"
        
        return [
            "tmux", "new-session", "-d", "-s", self.session_id,
            "bash", "-c", full_cmd
        ]
    
    async def start(self, task: str, context: Optional[str] = None) -> None:
        """Запустить worker с задачей"""
        from backend.services.bender.glm_client import clean_surrogates
        
        # Очищаем surrogate characters из задачи и контекста
        task = clean_surrogates(task)
        if context:
            context = clean_surrogates(context)
        
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
        # Для droid/codex exec передаём задачу в команду, для остальных — через send_input
        if self.WORKER_NAME in ("droid", "codex"):
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
            
            # Для droid/codex задача уже передана в команду
            if self.WORKER_NAME not in ("droid", "codex"):
                await asyncio.sleep(self.STARTUP_DELAY)
                await self.send_input(task)
                logger.info(f"[{self.WORKER_NAME}] Task sent to CLI")
            
        except Exception as e:
            logger.error(f"[{self.WORKER_NAME}] Failed to start: {e}")
            self.status = WorkerStatus.ERROR
            raise
    
    async def _start_native_terminal(self, task: str) -> None:
        """Запустить в нативном Terminal.app (visible режим)
        
        НАДЁЖНАЯ ДЕТЕКЦИЯ: пишем exit code в .done файл при завершении
        """
        from backend.services.bender.glm_client import clean_surrogates

        # Очищаем surrogate characters из задачи
        task = clean_surrogates(task)
        
        # ВАЖНО: Убиваем старые процессы с тем же session_id перед запуском
        # Это предотвращает дублирование при restart
        logger.info(f"[{self.WORKER_NAME}] Pre-start cleanup for session {self.session_id}")
        await self._cleanup_session_processes()
        
        # Создаём лог-файл, tty-файл и done-маркер
        self._log_file = Path(tempfile.gettempdir()) / f"{self.session_id}.log"
        self._done_file = Path(tempfile.gettempdir()) / f"{self.session_id}.done"
        self._terminal_tty_file = Path(tempfile.gettempdir()) / f"bender-tty-{self.session_id}.txt"
        
        # Удаляем старые файлы если есть
        if self._done_file.exists():
            self._done_file.unlink()
        if self._terminal_tty_file.exists():
            try:
                self._terminal_tty_file.unlink()
            except Exception:
                pass
        
        # Пишем задачу в файл
        task_file = Path(tempfile.gettempdir()) / f"bender-task-{self.session_id}.txt"
        task_file.write_text(task)
        
        # Создаём shell-скрипт с записью exit code в .done файл
        cli_cmd = shlex.join(self.cli_command)
        script_file = Path(tempfile.gettempdir()) / f"bender-run-{self.session_id}.sh"
        done_file_path = shlex.quote(str(self._done_file))
        log_file_path = shlex.quote(str(self._log_file))
        
        # Wrapper: запускаем через script для логов, но пишем exit code в .done
        # ВАЖНО: задача читается из файла чтобы избежать проблем с экранированием
        # кавычек, скобок и спецсимволов в тексте задачи
        task_file_escaped = shlex.quote(str(task_file))
        tty_file_escaped = shlex.quote(str(self._terminal_tty_file))
        if self.WORKER_NAME in ("copilot", "copilot-interactive"):
            # Для copilot: базовая команда БЕЗ задачи, задача добавляется из файла
            base_cmd = shlex.join(["copilot", "--allow-all", "--model", getattr(self, 'model', 'claude-opus-4.5')])
            # Создаём внутренний скрипт чтобы избежать проблем с вложенными кавычками
            inner_script = Path(tempfile.gettempdir()) / f"bender-inner-{self.session_id}.sh"
            inner_script_escaped = shlex.quote(str(inner_script))
            # ВАЖНО: используем -l для login shell чтобы загрузить PATH
            # ВАЖНО: copilot -p режим НЕ нужен script -q (это non-interactive!)
            inner_content = f'''#!/bin/bash -l
cd {shlex.quote(str(self.config.project_path))}
TTY=$(tty)
echo "$TTY" > {tty_file_escaped}
TASK=$(cat {task_file_escaped})
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🤖 BENDER → {self.WORKER_NAME}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "$TASK" | head -20
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
# Copilot -p режим non-interactive, не нужен script -q
# ВАЖНО: stdbuf -oL для line-buffered вывода в tee, sync для сброса буфера перед .done
{base_cmd} -p "$TASK" 2>&1 | tee -a {log_file_path}
EXIT_CODE=${{PIPESTATUS[0]}}
sync  # Сбросить файловые буферы перед записью .done
sleep 0.5  # Дать время на запись
echo $EXIT_CODE > {done_file_path}
'''
            inner_script.write_text(inner_content)
            inner_script.chmod(0o755)
            script_content = f'''#!/bin/bash -l
script -q {log_file_path} {inner_script_escaped}
'''
        elif self.WORKER_NAME == "droid":
            # droid: используем pipe с форматтером для читабельного вывода
            # Путь к форматтеру
            formatter_script = Path(__file__).parent.parent / "droid_stream_fmt.py"
            formatter_path = shlex.quote(str(formatter_script.resolve()))
            
            # Проверяем существует ли форматтер
            has_formatter = formatter_script.exists()
            
            # Используем ТЕКУЩИЙ интерпретатор (из venv) вместо python3
            current_python = shlex.quote(sys.executable)
            
            inner_script = Path(tempfile.gettempdir()) / f"bender-inner-{self.session_id}.sh"
            inner_script_escaped = shlex.quote(str(inner_script))
            
            # Получаем текущий PATH для прокидывания в tmux
            current_path = os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')
            
            # Строим pipeline:
            # 1. droid выводит JSON
            # 2. tee дублирует в лог (для Bender backend)
            # 3. formatter рисует красоту на экране
            pipe_cmd = f"| tee -a {log_file_path}"
            if has_formatter:
                # -u = unbuffered output (важно для real-time)
                # Используем sys.executable вместо python3
                pipe_cmd += f" | {current_python} -u {formatter_path}"
            
            inner_content = f'''#!/bin/bash -l
# ЯВНО ЭКСПОРТИРУЕМ PATH чтобы инструменты находились в tmux
export PATH="{current_path}:$PATH"

# === FIX: Signal Trap для корректного завершения всех процессов ===
# При выходе или сигнале убиваем ВСЕ дочерние процессы
cleanup() {{
    # Убиваем все фоновые джобы текущей оболочки
    jobs -p | xargs -r kill 2>/dev/null
    # Убиваем всю process group
    kill -- -$$ 2>/dev/null
}}
trap cleanup EXIT SIGINT SIGTERM

cd {shlex.quote(str(self.config.project_path))}
TTY=$(tty)
echo "$TTY" > {tty_file_escaped}
TASK=$(cat {task_file_escaped})
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🤖 BENDER → droid"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "$TASK" | head -20
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ВАЖНО: set -o pipefail чтобы получить exit code droid, а не formatter
set -o pipefail

# Запускаем pipeline в фоне чтобы иметь PID
{{ {cli_cmd} "$TASK" 2>&1 {pipe_cmd}; }} &
PIPELINE_PID=$!

# Ждем завершения pipeline
wait $PIPELINE_PID
EXIT_CODE=$?

sync
sleep 0.5
echo $EXIT_CODE > {done_file_path}
'''
            inner_script.write_text(inner_content)
            inner_script.chmod(0o755)
            script_content = f'''#!/bin/bash -l
script -q {log_file_path} {inner_script_escaped}
EXIT_CODE=$?
sync
sleep 0.5
echo $EXIT_CODE > {done_file_path}
'''
        else:
            # codex и другие
            # ВАЖНО: используем -l для login shell чтобы загрузить PATH
            # ВАЖНО: используем script -q для захвата TTY вывода + stdbuf для line-buffered
            script_content = f'''#!/bin/bash -l
cd {shlex.quote(str(self.config.project_path))}
TTY=$(tty)
echo "$TTY" > {tty_file_escaped}
TASK=$(cat {task_file_escaped})
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🤖 BENDER → {self.WORKER_NAME}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "$TASK" | head -20
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
# Используем script для захвата TTY вывода, stdbuf для line-buffered tee
script -q /dev/null {cli_cmd} "$TASK" 2>&1 | tee -a {log_file_path}
EXIT_CODE=${{PIPESTATUS[0]}}
sync  # Сбросить файловые буферы перед записью .done
sleep 0.5  # Дать время на запись
echo $EXIT_CODE > {done_file_path}
'''
        script_file.write_text(script_content)
        script_file.chmod(0o755)
        
        # AppleScript - открываем Terminal.app, сохраняем ID окна/вкладки и tty
        applescript = f'''
        tell application "Terminal"
            activate
            set t to do script "{script_file}"
            delay 0.3
            try
                set windowId to id of window of t
            on error
                try
                    set windowId to id of front window
                on error
                    set windowId to 0
                end try
            end try
            try
                set tabId to id of t
            on error
                try
                    set tabId to id of selected tab of front window
                on error
                    set tabId to 0
                end try
            end try
            try
                set tabTty to tty of t
            on error
                try
                    set tabTty to tty of selected tab of front window
                on error
                    set tabTty to ""
                end try
            end try
            try
                tell front window
                    set zoomed to false
                    set bounds to {{100, 100, 1000, 700}}
                end tell
            end try
            return (windowId as text) & "|" & (tabId as text) & "|" & tabTty
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
                raw = stdout.decode().strip()
                parts = raw.split("|")
                if parts:
                    if parts[0].strip().isdigit():
                        self._terminal_window_id = parts[0].strip()
                    if len(parts) > 1 and parts[1].strip().isdigit():
                        self._terminal_tab_id = parts[1].strip()
                    if len(parts) > 2 and parts[2].strip():
                        self._terminal_tty = parts[2].strip()
                logger.info(
                    f"[{self.WORKER_NAME}] Native terminal opened, window={self._terminal_window_id}, "
                    f"tab={self._terminal_tab_id}, tty={self._terminal_tty}"
                )
            else:
                logger.info(f"[{self.WORKER_NAME}] Native terminal opened")
            if stderr:
                err_text = stderr.decode(errors="replace").strip()
                if err_text:
                    logger.warning(f"[{self.WORKER_NAME}] AppleScript stderr: {err_text[:300]}")
            
            # Ждём пока процесс реально запустится и создаст лог
            await asyncio.sleep(3.0)  # Даём время на запуск

            # Пробуем прочитать tty из файла (fallback если AppleScript не вернул tty)
            if not self._terminal_tty and self._terminal_tty_file and self._terminal_tty_file.exists():
                try:
                    tty_value = self._terminal_tty_file.read_text(errors="replace").strip()
                    if tty_value:
                        self._terminal_tty = tty_value
                        logger.info(f"[{self.WORKER_NAME}] Loaded tty from file: {self._terminal_tty}")
                except Exception as e:
                    logger.debug(f"[{self.WORKER_NAME}] Failed to read tty file: {e}")
            
            # Находим PIDs процессов которые МЫ запустили (по session_id в командной строке)
            try:
                find_pids = await asyncio.create_subprocess_shell(
                    f"pgrep -f '{self.session_id}'",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL
                )
                stdout, _ = await find_pids.communicate()
                if stdout:
                    for pid_str in stdout.decode().strip().split('\n'):
                        if pid_str.strip().isdigit():
                            self._spawned_pids.add(int(pid_str.strip()))
                    if self._spawned_pids:
                        logger.info(f"[{self.WORKER_NAME}] Tracking spawned PIDs: {self._spawned_pids}")
            except Exception as e:
                logger.warning(f"[{self.WORKER_NAME}] Failed to track PIDs: {e}")
            
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
            # Background mode: убить tmux сессию И все связанные процессы
            try:
                # Сначала убиваем все процессы связанные с сессией
                await self._cleanup_session_processes()
                
                # Потом убиваем tmux сессию
                process = await asyncio.create_subprocess_exec(
                    "tmux", "kill-session", "-t", self.session_id,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await process.wait()
            except Exception as e:
                logger.warning(f"[{self.WORKER_NAME}] Error stopping session: {e}")
        
        # Unregister atexit handler to prevent memory leak across worker lifecycles
        atexit.unregister(self._cleanup_temp_files_sync)

        self.status = WorkerStatus.IDLE
        self.current_task = None

    async def _cleanup_session_processes(self) -> None:
        """Убить процессы связанные с ЭТОЙ сессией
        
        Стратегия:
        1. Сначала убиваем PIDs которые МЫ запустили (_spawned_pids)
        2. Потом ищем процессы по session_id (fallback)
        """
        killed_pids = set()
        
        # 1. Убиваем НАШИ процессы (из _spawned_pids) - БЕЗОПАСНО
        if self._spawned_pids:
            logger.info(f"[{self.WORKER_NAME}] Killing {len(self._spawned_pids)} tracked PIDs: {self._spawned_pids}")
            for pid in self._spawned_pids:
                try:
                    kill_proc = await asyncio.create_subprocess_exec(
                        "kill", "-15", str(pid),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL
                    )
                    await kill_proc.wait()
                    killed_pids.add(pid)
                except Exception:
                    pass
            
            await asyncio.sleep(0.5)
            
            # Force kill оставшихся
            for pid in self._spawned_pids:
                try:
                    # Проверяем жив ли процесс
                    check = await asyncio.create_subprocess_exec(
                        "kill", "-0", str(pid),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL
                    )
                    if await check.wait() == 0:  # Процесс ещё жив
                        kill_proc = await asyncio.create_subprocess_exec(
                            "kill", "-9", str(pid),
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL
                        )
                        await kill_proc.wait()
                        logger.info(f"[{self.WORKER_NAME}] Force killed PID {pid}")
                except Exception:
                    pass
            
            self._spawned_pids.clear()
        
        # 2. Fallback: ищем по session_id (менее безопасно, но нужно для cleanup)
        try:
            find_proc = await asyncio.create_subprocess_shell(
                f"pgrep -f '{self.session_id}'",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            stdout, _ = await find_proc.communicate()
            
            if stdout:
                pids = [int(p.strip()) for p in stdout.decode().strip().split('\n') 
                        if p.strip().isdigit() and int(p.strip()) not in killed_pids]
                if pids:
                    logger.info(f"[{self.WORKER_NAME}] Found {len(pids)} additional session processes: {pids}")
                    
                    for pid in pids:
                        try:
                            kill_proc = await asyncio.create_subprocess_exec(
                                "kill", "-15", str(pid),
                                stdout=asyncio.subprocess.DEVNULL,
                                stderr=asyncio.subprocess.DEVNULL
                            )
                            await kill_proc.wait()
                        except Exception:
                            pass
                    
                    await asyncio.sleep(0.5)
                    
                    # Force kill оставшихся
                    check_proc = await asyncio.create_subprocess_shell(
                        f"pgrep -f '{self.session_id}'",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL
                    )
                    stdout, _ = await check_proc.communicate()
                    
                    if stdout:
                        remaining = [p.strip() for p in stdout.decode().strip().split('\n') if p.strip().isdigit()]
                        for pid in remaining:
                            try:
                                kill_proc = await asyncio.create_subprocess_exec(
                                    "kill", "-9", pid,
                                    stdout=asyncio.subprocess.DEVNULL,
                                    stderr=asyncio.subprocess.DEVNULL
                                )
                                await kill_proc.wait()
                            except Exception:
                                pass
        except Exception as e:
            logger.warning(f"[{self.WORKER_NAME}] Error in fallback cleanup: {e}")

        # 3. Final fallback: убиваем процессы по tty (если есть)
        if not self._terminal_tty and self._terminal_tty_file and self._terminal_tty_file.exists():
            try:
                tty_value = self._terminal_tty_file.read_text(errors="replace").strip()
                if tty_value:
                    self._terminal_tty = tty_value
            except Exception:
                pass

        if self._terminal_tty:
            tty_name = self._terminal_tty.replace("/dev/", "").strip()
            if tty_name:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "ps", "-t", tty_name, "-o", "pid=",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL
                    )
                    stdout, _ = await proc.communicate()
                    if stdout:
                        tty_pids = [p.strip() for p in stdout.decode().strip().split('\n') if p.strip().isdigit()]
                        for pid in tty_pids:
                            try:
                                kill_proc = await asyncio.create_subprocess_exec(
                                    "kill", "-15", pid,
                                    stdout=asyncio.subprocess.DEVNULL,
                                    stderr=asyncio.subprocess.DEVNULL
                                )
                                await kill_proc.wait()
                                killed_pids.add(int(pid))
                            except Exception:
                                pass
                        await asyncio.sleep(0.3)
                        # Force kill оставшихся
                        for pid in tty_pids:
                            if pid and pid.isdigit():
                                try:
                                    check = await asyncio.create_subprocess_exec(
                                        "kill", "-0", pid,
                                        stdout=asyncio.subprocess.DEVNULL,
                                        stderr=asyncio.subprocess.DEVNULL
                                    )
                                    if await check.wait() == 0:
                                        kill_proc = await asyncio.create_subprocess_exec(
                                            "kill", "-9", pid,
                                            stdout=asyncio.subprocess.DEVNULL,
                                            stderr=asyncio.subprocess.DEVNULL
                                        )
                                        await kill_proc.wait()
                                except Exception:
                                    pass
                except Exception as e:
                    logger.warning(f"[{self.WORKER_NAME}] Error cleaning tty processes: {e}")
    
    async def _close_native_terminal(self) -> None:
        """Закрыть нативное окно терминала
        
        УЛУЧШЕННАЯ ВЕРСИЯ: fallback закрытие по session_id если нет window_id
        """
        # КРИТИЧНО: Читаем лог ПЕРЕД любым cleanup!
        # Это гарантирует что мы получим полный вывод
        if self._log_file is not None and self._log_file.exists():
            try:
                final_output = self._log_file.read_text(errors='replace')
                if len(final_output) > len(getattr(self, '_output', '') or ''):
                    self._output = final_output
                    logger.info(f"[{self.WORKER_NAME}] Captured final output before close: {len(self._output)} chars")
            except Exception as e:
                logger.warning(f"[{self.WORKER_NAME}] Failed to read final log: {e}")
        
        # Убиваем ВСЕ процессы сессии
        await self._cleanup_session_processes()
        
        if sys.platform == "darwin":
            window_id = getattr(self, '_terminal_window_id', None)
            tab_id = getattr(self, '_terminal_tab_id', None)
            tab_tty = getattr(self, '_terminal_tty', None)
            
            await asyncio.sleep(0.3)
            
            closed = False
            
            # СПОСОБ 1: Закрыть по tab_id (самый точный, не трогает другие вкладки)
            if tab_id:
                script = f'''
                tell application "Terminal"
                    try
                        repeat with w in windows
                            repeat with t in tabs of w
                                if id of t is {tab_id} then
                                    close t saving no
                                    return "ok"
                                end if
                            end repeat
                        end repeat
                        return "fail"
                    on error
                        return "fail"
                    end try
                end tell
                '''
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "osascript", "-e", script,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL
                    )
                    stdout, _ = await proc.communicate()
                    if stdout and "ok" in stdout.decode():
                        closed = True
                        logger.info(f"[{self.WORKER_NAME}] Closed terminal tab {tab_id}")
                except Exception as e:
                    logger.warning(f"[{self.WORKER_NAME}] Failed to close tab {tab_id}: {e}")
            
            # СПОСОБ 2: Закрыть по tty (если tab_id не найден)
            if not closed and tab_tty:
                safe_tty = tab_tty.replace('"', '')
                script = f'''
                tell application "Terminal"
                    try
                        repeat with w in windows
                            repeat with t in tabs of w
                                try
                                    if tty of t is "{safe_tty}" then
                                        close t saving no
                                        return "ok"
                                    end if
                                end try
                            end repeat
                        end repeat
                        return "fail"
                    on error
                        return "fail"
                    end try
                end tell
                '''
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "osascript", "-e", script,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL
                    )
                    stdout, _ = await proc.communicate()
                    if stdout and "ok" in stdout.decode():
                        closed = True
                        logger.info(f"[{self.WORKER_NAME}] Closed terminal tab by tty {tab_tty}")
                except Exception as e:
                    logger.warning(f"[{self.WORKER_NAME}] Failed to close tab by tty {tab_tty}: {e}")
            
            # СПОСОБ 3: Закрыть по window_id (менее точный)
            if not closed and window_id:
                script = f'''
                tell application "Terminal"
                    try
                        close (first window whose id is {window_id}) saving no
                        return "ok"
                    on error
                        return "fail"
                    end try
                end tell
                '''
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "osascript", "-e", script,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL
                    )
                    stdout, _ = await proc.communicate()
                    if stdout and "ok" in stdout.decode():
                        closed = True
                        logger.info(f"[{self.WORKER_NAME}] Closed terminal window {window_id}")
                except Exception as e:
                    logger.warning(f"[{self.WORKER_NAME}] Failed to close window {window_id}: {e}")
            
            # СПОСОБ 4: Fallback - закрыть окна с session_id в названии (менее безопасно)
            if not closed:
                logger.warning(f"[{self.WORKER_NAME}] No tab/window id or close failed, trying fallback by session_id")
                fallback_script = f'''
                tell application "Terminal"
                    set closedCount to 0
                    repeat with w in windows
                        try
                            -- Проверяем что имя окна содержит наш session_id
                            if name of w contains "{self.session_id}" then
                                close w saving no
                                set closedCount to closedCount + 1
                            end if
                        end try
                    end repeat
                    return closedCount as text
                end tell
                '''
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "osascript", "-e", fallback_script,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL
                    )
                    stdout, _ = await proc.communicate()
                    if stdout:
                        try:
                            closed_count = int(stdout.decode().strip())
                            if closed_count > 0:
                                logger.info(f"[{self.WORKER_NAME}] Fallback closed {closed_count} window(s) by session_id")
                                closed = True
                        except ValueError:
                            pass
                except Exception as e:
                    logger.warning(f"[{self.WORKER_NAME}] Fallback close failed: {e}")
            
            if not closed:
                logger.error(f"[{self.WORKER_NAME}] FAILED to close terminal - may cause leak! session={self.session_id}")
        
        # Удалить временные файлы
        for pattern in ["bender-task-", "bender-run-", "bender-winid-", "bender-inner-", "bender-tty-", "bender-"]:
            temp_file = Path(tempfile.gettempdir()) / f"{pattern}{self.session_id}"
            for suffix in ["", ".txt", ".sh", ".log", ".done"]:
                f = Path(str(temp_file) + suffix)
                if f.exists():
                    try:
                        f.unlink()
                        logger.debug(f"[{self.WORKER_NAME}] Deleted temp file: {f.name}")
                    except Exception:
                        pass
    
    async def capture_output(self) -> str:
        """Захватить текущий вывод (из лог-файла, tmux или Terminal.app)"""
        # 1. Пробуем лог-файл
        if self._log_file is not None and self._log_file.exists():
            try:
                content = self._log_file.read_text(errors='replace')
                if content.strip():
                    return content
            except Exception:
                pass
        
        # 2. Visible mode с Terminal.app: захватываем через AppleScript
        if self.config.visible:
            # 2.1 По window_id
            if self._terminal_window_id:
                try:
                    script = f'''
                    tell application "Terminal"
                        try
                            set w to first window whose id is {self._terminal_window_id}
                            return contents of first tab of w
                        end try
                    end tell
                    '''
                    process = await asyncio.create_subprocess_exec(
                        "osascript", "-e", script,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    stdout, _ = await process.communicate()
                    if stdout:
                        return stdout.decode("utf-8", errors="replace")
                except Exception as e:
                    logger.debug(f"[{self.WORKER_NAME}] Terminal capture (window_id) failed: {e}")
            
            # 2.2 По tab_id
            if self._terminal_tab_id:
                try:
                    script = f'''
                    tell application "Terminal"
                        try
                            repeat with w in windows
                                repeat with t in tabs of w
                                    if id of t is {self._terminal_tab_id} then
                                        return contents of t
                                    end if
                                end repeat
                            end repeat
                        end try
                    end tell
                    '''
                    process = await asyncio.create_subprocess_exec(
                        "osascript", "-e", script,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    stdout, _ = await process.communicate()
                    if stdout:
                        return stdout.decode("utf-8", errors="replace")
                except Exception as e:
                    logger.debug(f"[{self.WORKER_NAME}] Terminal capture (tab_id) failed: {e}")
            
            # 2.3 По tty (fallback)
            if not self._terminal_tty and self._terminal_tty_file and self._terminal_tty_file.exists():
                try:
                    tty_value = self._terminal_tty_file.read_text(errors="replace").strip()
                    if tty_value:
                        self._terminal_tty = tty_value
                except Exception:
                    pass
            if self._terminal_tty:
                safe_tty = self._terminal_tty.replace('"', '')
                try:
                    script = f'''
                    tell application "Terminal"
                        try
                            repeat with w in windows
                                repeat with t in tabs of w
                                    try
                                        if tty of t is "{safe_tty}" then
                                            return contents of t
                                        end if
                                    end try
                                end repeat
                            end repeat
                        end try
                    end tell
                    '''
                    process = await asyncio.create_subprocess_exec(
                        "osascript", "-e", script,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    stdout, _ = await process.communicate()
                    if stdout:
                        return stdout.decode("utf-8", errors="replace")
                except Exception as e:
                    logger.debug(f"[{self.WORKER_NAME}] Terminal capture (tty) failed: {e}")
        
        # 3. Fallback: tmux (для невидимого режима)
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
        """Проверить, жива ли сессия
        
        НАДЁЖНАЯ ДЕТЕКЦИЯ: проверяем .done файл
        """
        # 1. Самый надёжный способ: проверяем .done файл
        done_file = getattr(self, '_done_file', None)
        if done_file and done_file.exists():
            try:
                exit_code = int(done_file.read_text().strip())
                logger.info(f"[{self.WORKER_NAME}] Done file found, exit code: {exit_code}")
                return False  # Session completed
            except (ValueError, IOError):
                pass
        
        if self.config.visible:
            # Visible mode: проверяем что процесс script ещё работает
            try:
                process = await asyncio.create_subprocess_exec(
                    "pgrep", "-f", self.session_id,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                returncode = await process.wait()
                return returncode == 0
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

    async def event_stream(self, timeout: float = 1800) -> AsyncIterator[UniversalEvent]:
        """Стримить события в реальном времени.

        Заменяет wait_for_completion() — вместо (bool, str) yield-ит UniversalEvent.

        Логика:
        1. Создать adapter через _create_streaming_adapter()
        2. Yield SESSION_START
        3. В цикле: читать лог файл / process output инкрементально
        4. Конвертировать новые chunk-и текста через adapter.feed_text()
        5. Yield каждый UniversalEvent
        6. При завершении (exit code / patterns / timeout): yield SESSION_END

        Args:
            timeout: Максимальное время ожидания (секунды)

        Yields:
            UniversalEvent с типами: SESSION_START, ITEM_START, ITEM_DELTA, ITEM_END, SESSION_END
        """
        from backend.core.events.schema import UniversalEvent

        adapter = self._create_streaming_adapter()

        # Start session
        session_event = adapter.start_session(
            agent_id=f"{self.WORKER_NAME}-{self.session_id}",
            session_id=self.session_id,
        )
        yield session_event

        start = asyncio.get_event_loop().time()
        check_interval = self.effective_interval
        last_output_len = 0
        self._completed = False
        self._output = ""

        try:
            while asyncio.get_event_loop().time() - start < timeout:
                await asyncio.sleep(min(check_interval, 2.0))  # Чаще для streaming

                # Читаем текущий output (из лог файла или capture)
                current_output = ""
                if self._log_file is not None and self._log_file.exists():
                    try:
                        current_output = self._log_file.read_text(errors='replace')
                    except OSError:
                        current_output = ""
                else:
                    current_output = await self.capture_output()

                # Есть новые данные?
                if len(current_output) > last_output_len:
                    new_text = current_output[last_output_len:]
                    last_output_len = len(current_output)

                    # Конвертировать через adapter
                    try:
                        events = adapter.feed_text(new_text)
                        for event in events:
                            yield event
                    except Exception as e:
                        logger.warning(
                            "[%s] adapter.feed_text() error: %s", self.WORKER_NAME, e
                        )

                # Проверить завершение
                session_alive = await self.is_session_alive()
                if not session_alive:
                    self._completed = True
                    self._output = current_output
                    self.status = WorkerStatus.COMPLETED
                    break

                # Detect completion patterns
                completion_reason = self.detect_completion(current_output)
                if completion_reason:
                    self._completed = True
                    self._output = current_output
                    self.status = WorkerStatus.COMPLETED
                    break

                # Detect stuck
                if self.detect_stuck(current_output):
                    self.status = WorkerStatus.STUCK
                    break

        finally:
            # End session
            end_events = adapter.end_session()
            for event in end_events:
                yield event

    async def attach(self) -> None:
        """Присоединиться к tmux сессии (для --visible)"""
        subprocess.run(["tmux", "attach-session", "-t", self.session_id])
