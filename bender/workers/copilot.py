"""
Copilot Worker - основной worker для GitHub Copilot CLI

НАДЁЖНАЯ ДЕТЕКЦИЯ:
- Non-interactive (`-p`): процесс завершается сам с exit code
- Interactive (visible): паттерны + `--share` для сохранения результата
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


def cleanup_orphaned_processes() -> dict:
    """Убить orphaned BENDER процессы (ТОЛЬКО связанные с bender!)
    
    ВАЖНО: Убиваем ТОЛЬКО процессы с "bender" в командной строке или путях!
    НЕ трогаем обычные copilot/codex процессы пользователя.
    
    Ищет и убивает:
    - Старые bender-run-* bash обертки
    - script процессы запущенные bender (PTY leak fix!)
    - bender-inner-* скрипты
    - Закрывает orphaned Terminal окна с "BENDER" в названии
    
    Returns:
        dict с информацией о cleanup
    """
    import subprocess
    import time
    
    killed_processes = []
    closed_windows = []
    
    # 0. Убиваем старые bender tmux сессии
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            for session in result.stdout.strip().split('\n'):
                if session.startswith('bender-'):
                    try:
                        subprocess.run(["tmux", "kill-session", "-t", session], timeout=2)
                        killed_processes.append(f"tmux:{session}")
                        logger.info(f"Killed orphaned tmux session: {session}")
                    except Exception:
                        pass
    except Exception:
        pass  # tmux может быть не запущен
    
    # 1. Убиваем bender-run-* и bender-inner-* bash обертки
    for pattern in ["bender-run-", "bender-inner-", "bender-task-"]:
        try:
            result = subprocess.run(
                ["pgrep", "-f", pattern],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode == 0:
                pids = result.stdout.strip().split('\n')
                for pid in pids:
                    if pid.strip():
                        try:
                            subprocess.run(["kill", "-9", pid.strip()], timeout=2)
                            killed_processes.append(f"{pattern} (PID {pid.strip()})")
                            logger.info(f"Killed orphaned {pattern} process: PID {pid.strip()}")
                        except Exception:
                            pass
        except Exception as e:
            logger.warning(f"Error cleaning {pattern} processes: {e}")
    
    # 2. КРИТИЧНО: Убиваем script процессы которые держат PTY (только bender-related!)
    # Ищем script процессы которые запущены с bender session ID
    try:
        # Получаем все script процессы с их командной строкой
        result = subprocess.run(
            ["ps", "-eo", "pid,command"],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                # Ищем script процессы связанные с bender
                # Проверяем: bender- в пути, /tmp/bender, или /var/folders/.../bender-
                if 'script' in line and ('bender-' in line or '/tmp/bender' in line or 'bender-droid' in line or 'bender-copilot' in line or 'bender-codex' in line):
                    parts = line.strip().split(None, 1)
                    if parts and parts[0].isdigit():
                        pid = parts[0]
                        try:
                            subprocess.run(["kill", "-9", pid], timeout=2)
                            killed_processes.append(f"script (PID {pid})")
                            logger.info(f"Killed orphaned script process: PID {pid}")
                        except Exception:
                            pass
    except Exception as e:
        logger.warning(f"Error cleaning script processes: {e}")
    
    # 2. Закрываем orphaned Terminal окна ТОЛЬКО с точными bender паттернами (только на macOS)
    # ВАЖНО: НЕ закрываем окна просто содержащие "bender" - только специфичные bender окна!
    try:
        import sys
        if sys.platform == "darwin":
            # Ищем окна Terminal ТОЛЬКО с точными bender паттернами
            # "BENDER →" - visible mode header
            # "bender-run-" или "bender-droid-" - session scripts
            script = '''
            tell application "Terminal"
                set windowList to {}
                repeat with w in windows
                    try
                        set wName to name of w
                        if wName contains "BENDER →" or wName contains "bender-run-" or wName contains "bender-droid-" or wName contains "bender-copilot-" or wName contains "bender-codex-" then
                            set end of windowList to id of w
                        end if
                    end try
                end repeat
                return windowList
            end tell
            '''
            
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode == 0 and result.stdout.strip():
                window_ids = result.stdout.strip().split(', ')
                for wid in window_ids:
                    if wid.strip():
                        close_script = f'''
                        tell application "Terminal"
                            try
                                close (first window whose id is {wid.strip()}) saving no
                            end try
                        end tell
                        '''
                        try:
                            subprocess.run(["osascript", "-e", close_script], timeout=2)
                            closed_windows.append(f"Terminal window {wid.strip()}")
                            logger.info(f"Closed orphaned Terminal window: {wid.strip()}")
                        except Exception:
                            pass
    except Exception as e:
        logger.warning(f"Error closing Terminal windows: {e}")
    
    return {
        "killed_processes": killed_processes,
        "closed_windows": closed_windows,
        "total_killed": len(killed_processes),
        "total_closed": len(closed_windows)
    }


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
    
    НАДЁЖНАЯ детекция завершения:
    1. Non-interactive (-p): процесс завершается сам с exit code
    2. Interactive (visible): паттерны + проверка exit code
    
    Copilot -p выполняет задачу и ЗАВЕРШАЕТСЯ - это надёжнее паттернов!
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
        model: str = "claude-opus-4.6", 
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

    def _create_streaming_adapter(self):
        """CopilotWorker использует GenericStreamingAdapter из base.py.

        Returns:
            GenericStreamingAdapter для plain text output
        """
        from .base import GenericStreamingAdapter

        return GenericStreamingAdapter(agent_type="copilot")
    
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
        
        # Логируем полный промпт для отладки (первые 500 символов)
        # Очищаем surrogates чтобы избежать UnicodeEncodeError в логах
        from backend.services.bender.glm_client import clean_surrogates
        prompt_preview = formatted_task[:500] + "..." if len(formatted_task) > 500 else formatted_task
        prompt_preview = clean_surrogates(prompt_preview)
        logger.debug(f"[{self.WORKER_NAME}] Full prompt preview:\n{prompt_preview}")
        
        if self.visible:
            # Visible mode - используем native Terminal.app (как droid)
            await self._start_native_terminal(formatted_task)
        else:
            # Background mode - subprocess
            cmd = self.cli_command
            logger.debug(f"[{self.WORKER_NAME}] Command: {cmd}")
            await self._start_background(cmd)
    
    async def _start_background(self, cmd: List[str]) -> None:
        """Запустить в фоне через subprocess (локально или через SSH)"""
        try:
            if self.config.ssh_host:
                # Remote execution via SSH
                import shlex
                remote_cmd = f"cd {shlex.quote(str(self.config.project_path))} && {' '.join(shlex.quote(c) for c in cmd)}"
                final_cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", self.config.ssh_host, remote_cmd]
                logger.info(f"[{self.WORKER_NAME}] Remote SSH: {self.config.ssh_host}:{self.config.project_path}")
                logger.info(f"[{self.WORKER_NAME}] Running: {' '.join(final_cmd[:4])}...")
                self._process = await asyncio.create_subprocess_exec(
                    *final_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            else:
                # Local execution
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
        # Visible mode - читаем из лог-файла
        if self.visible:
            if self._log_file is not None and self._log_file.exists():
                try:
                    return self._log_file.read_text(errors='replace')
                except Exception:
                    pass
            return self._output
        
        # Background mode - читаем из process stdout
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
        
        Non-visible mode: activity-based timeout.
        Убиваем только если вывод не менялся inactivity_timeout секунд.
        Общий лимит — max_total_timeout (дефолт 8ч).
        
        Args:
            timeout: inactivity timeout (сколько секунд тишины = зависание)
        
        Returns:
            Tuple[success, output]
        """
        # Visible mode - используем логику с маркерами
        if self.visible:
            return await self._wait_visible(timeout)
        
        if self._process is None:
            return False, ""
        
        inactivity_timeout = timeout  # 300s по умолчанию
        max_total = getattr(self.config, 'max_total_timeout', 28800.0)
        check_interval = 5.0  # Проверяем каждые 5 секунд
        
        start_time = asyncio.get_event_loop().time()
        last_output_len = 0
        last_activity_time = start_time
        
        try:
            while True:
                now = asyncio.get_event_loop().time()
                elapsed = now - start_time
                inactive_for = now - last_activity_time
                
                # Проверяем общий таймаут (8ч)
                if elapsed >= max_total:
                    logger.warning(f"[{self.WORKER_NAME}] Max total timeout {max_total}s reached")
                    self.status = WorkerStatus.STUCK
                    return False, self._output
                
                # Проверяем inactivity таймаут
                if inactive_for >= inactivity_timeout:
                    logger.warning(
                        f"[{self.WORKER_NAME}] Inactivity timeout: no output for "
                        f"{int(inactive_for)}s (limit {int(inactivity_timeout)}s), "
                        f"total elapsed {int(elapsed)}s"
                    )
                    self.status = WorkerStatus.STUCK
                    return False, self._output
                
                # Проверяем: процесс завершился?
                if self._process.returncode is not None:
                    # Процесс вышел — собираем оставшийся stdout
                    remaining = b""
                    if self._process.stdout:
                        try:
                            remaining = await asyncio.wait_for(
                                self._process.stdout.read(), timeout=2.0
                            )
                        except (asyncio.TimeoutError, Exception):
                            pass
                    if remaining:
                        self._output += remaining.decode('utf-8', errors='replace')
                    
                    self._completed = True
                    self.status = WorkerStatus.COMPLETED
                    self.token_usage = self._parse_token_usage(self._output)
                    if self.token_usage:
                        logger.info(f"[{self.WORKER_NAME}] {self.token_usage}")
                    logger.info(
                        f"[{self.WORKER_NAME}] Completed with {len(self._output)} chars "
                        f"output in {int(elapsed)}s"
                    )
                    return True, self._output
                
                # Читаем stdout chunk (non-blocking)
                if self._process.stdout:
                    try:
                        chunk = await asyncio.wait_for(
                            self._process.stdout.read(8192),
                            timeout=check_interval,
                        )
                        if chunk:
                            self._output += chunk.decode('utf-8', errors='replace')
                        elif self._process.returncode is not None:
                            # EOF + process exited — следующая итерация обработает
                            continue
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(check_interval)
                
                # Отслеживаем активность
                current_len = len(self._output)
                if current_len != last_output_len:
                    last_output_len = current_len
                    last_activity_time = asyncio.get_event_loop().time()
                
        except Exception as e:
            logger.error(f"[{self.WORKER_NAME}] Error waiting: {e}")
            self.status = WorkerStatus.ERROR
            return False, str(e)
    
    async def _wait_visible(self, timeout: float) -> Tuple[bool, str]:
        """Дождаться завершения в visible mode
        
        Проверяем .done файл каждые 5 секунд — это самый надёжный способ.
        """
        start = asyncio.get_event_loop().time()
        check_interval = 5  # Проверка .done файла каждые 5 секунд
        llm_interval = 60   # LLM анализ каждые 60 секунд
        last_llm_check = 0
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
            
            # 1. САМЫЙ НАДЁЖНЫЙ: проверяем .done файл напрямую
            done_file = getattr(self, '_done_file', None)
            if done_file and done_file.exists():
                try:
                    exit_code = int(done_file.read_text().strip())
                    # ВАЖНО: ждём пока лог перестанет расти (tee дописывает)
                    # Читаем несколько раз с интервалом пока размер стабилизируется
                    last_size = 0
                    stable_count = 0
                    for _ in range(10):  # max 5 секунд (10 * 0.5)
                        await asyncio.sleep(0.5)
                        if self._log_file is not None and self._log_file.exists():
                            current_output = self._log_file.read_text(errors='replace')
                            current_size = len(current_output)
                            if current_size == last_size:
                                stable_count += 1
                                if stable_count >= 2:  # 2 раза подряд одинаковый размер
                                    break
                            else:
                                stable_count = 0
                                last_size = current_size
                    
                    logger.info(f"[{self.WORKER_NAME}] Log stabilized at {len(current_output)} chars after {stable_count} stable reads")
                    self._completed = True
                    self._output = current_output
                    self.status = WorkerStatus.COMPLETED
                    self.token_usage = self._parse_token_usage(self._output)
                    logger.info(f"[{self.WORKER_NAME}] Done file found, exit code: {exit_code}, output: {len(self._output)} chars")
                    return exit_code == 0, self._output
                except (ValueError, IOError):
                    pass
            
            # 2. Детекция по паттернам (быстрая, без LLM)
            completion_reason = self.detect_completion(current_output)
            if completion_reason:
                self._completed = True
                self._output = current_output
                self.status = WorkerStatus.COMPLETED
                self.token_usage = self._parse_token_usage(self._output)
                logger.info(f"[{self.WORKER_NAME}] Completed: {completion_reason}")
                return True, self._output
            
            # 3. НЕ проверяем stuck здесь - полагаемся на .done файл и timeout
            # Copilot может долго думать без вывода, это нормально
            
            # 4. LLM анализ (если есть) — только каждые 60 секунд
            if self._llm_analyze and len(current_output) > 100 and elapsed - last_llm_check >= llm_interval:
                last_llm_check = elapsed
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
