"""
Copilot Worker - основной worker для GitHub Copilot CLI
"""

import asyncio
import logging
import re
import shlex
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .base import BaseWorker, WorkerConfig, WorkerStatus

logger = logging.getLogger(__name__)


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
    
    # Паттерны для парсинга статистики из вывода copilot
    TOKEN_PATTERN = re.compile(
        r'(\w[\w\-\.]+)\s+([\d.]+)k\s+in,\s+([\d.]+)\s+out,\s+([\d.]+)k\s+cached'
    )
    API_TIME_PATTERN = re.compile(r'API time spent:\s+(\d+)s')
    TOTAL_TIME_PATTERN = re.compile(r'Total session time:\s+(\d+)s')
    PREMIUM_PATTERN = re.compile(r'(\d+)\s+Premium request')
    
    def __init__(self, config: WorkerConfig, model: str = "claude-sonnet-4", visible: bool = False):
        super().__init__(config)
        self.model = model
        self.visible = visible
        self._pending_task: Optional[str] = None
        self._output: str = ""
        self._completed: bool = False
        self.token_usage: Optional[TokenUsage] = None
        self._tmux_session: Optional[str] = None
    
    @property
    def cli_command(self) -> List[str]:
        """CLI команда для copilot"""
        cmd = [
            "copilot",
            "--allow-all-tools",
            "--model", self.model,
        ]
        # Non-interactive mode с задачей
        if self._pending_task:
            cmd.extend(["-p", self._pending_task])
        return cmd
    
    def format_task(self, task: str, context: Optional[str] = None) -> str:
        """Форматировать задачу для copilot"""
        if context:
            full_task = f"{task}\n\nКонтекст предыдущей работы:\n{context}"
        else:
            full_task = task
        self._pending_task = full_task
        return full_task
    
    async def start(self, task: str, context: Optional[str] = None) -> None:
        """Запустить copilot с задачей
        
        Copilot в режиме -p выполняет задачу и завершается.
        В visible mode запускается в tmux для отображения.
        """
        self.current_task = task
        self.status = WorkerStatus.RUNNING
        self.start_time = __import__('time').time()
        self._output = ""
        self._completed = False
        
        # Форматируем задачу
        formatted_task = self.format_task(task, context)
        logger.info(f"[{self.WORKER_NAME}] Starting: {task[:50]}...")
        
        cmd = self.cli_command
        logger.debug(f"[{self.WORKER_NAME}] Command: {cmd}")
        
        if self.visible:
            # Visible mode - запускаем в tmux для отображения
            await self._start_visible(cmd)
        else:
            # Background mode - subprocess
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
    
    async def _start_visible(self, cmd: List[str]) -> None:
        """Запустить в tmux для отображения"""
        import uuid
        self._tmux_session = f"bender-copilot-{uuid.uuid4().hex[:8]}"
        
        # Создаём лог файл для захвата вывода
        self._log_file = f"/tmp/{self._tmux_session}.log"
        
        # Команда с логированием в файл (без паузы - закроется автоматически)
        cmd_str = " ".join(shlex.quote(c) for c in cmd)
        full_cmd = f"cd {shlex.quote(str(self.config.project_path))} && echo '🤖 Bender visible mode - copilot running...' && echo '' && {cmd_str} 2>&1 | tee {self._log_file}"
        
        try:
            # Создаём tmux сессию
            proc = await asyncio.create_subprocess_exec(
                "tmux", "new-session", "-d", "-s", self._tmux_session,
                "bash", "-c", full_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()
            
            logger.info(f"[{self.WORKER_NAME}] Tmux session started: {self._tmux_session}")
            logger.info(f"[{self.WORKER_NAME}] Attach with: tmux attach -t {self._tmux_session}")
            
            # Сохраняем window ID для закрытия потом
            self._terminal_window_id = None
            
            # Открываем в новом окне терминала (macOS) в правильной директории
            try:
                project_path = str(self.config.project_path)
                # Создаём новое окно и получаем его ID
                applescript = f'''
                tell application "Terminal"
                    activate
                    set newTab to do script "cd {project_path} && tmux attach -t {self._tmux_session}; exit"
                    set newWindow to window 1
                    return id of newWindow
                end tell
                '''
                attach_proc = await asyncio.create_subprocess_exec(
                    "osascript", "-e", applescript,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await attach_proc.communicate()
                if stdout:
                    self._terminal_window_id = stdout.decode().strip()
                    logger.info(f"[{self.WORKER_NAME}] Terminal window ID: {self._terminal_window_id}")
                logger.info(f"[{self.WORKER_NAME}] Opened Terminal window in {project_path}")
            except Exception as e:
                # Fallback: просто логируем команду для ручного подключения
                logger.warning(f"[{self.WORKER_NAME}] Could not open Terminal: {e}")
                logger.info(f"[{self.WORKER_NAME}] Open terminal manually: tmux attach -t {self._tmux_session}")
            
        except Exception as e:
            logger.error(f"[{self.WORKER_NAME}] Failed to start tmux: {e}")
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
        if self.visible and self._tmux_session:
            # Проверяем tmux сессию
            proc = await asyncio.create_subprocess_exec(
                "tmux", "has-session", "-t", self._tmux_session,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            ret = await proc.wait()
            if ret != 0:
                self._completed = True
                return False
            return True
        
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
        if self.visible and self._tmux_session:
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
        """Дождаться завершения в visible mode (tmux)
        
        Проверяем лог файл на наличие маркера завершения copilot.
        """
        import os
        start = asyncio.get_event_loop().time()
        
        # Маркеры завершения copilot
        completion_markers = [
            "Total usage est:",
            "Total session time:",
            "Breakdown by AI model:",
        ]
        
        while asyncio.get_event_loop().time() - start < timeout:
            # Читаем текущий лог
            if hasattr(self, '_log_file') and os.path.exists(self._log_file):
                try:
                    with open(self._log_file, 'r') as f:
                        log_content = f.read()
                    
                    # Проверяем есть ли маркеры завершения
                    if any(marker in log_content for marker in completion_markers):
                        self._completed = True
                        self.status = WorkerStatus.COMPLETED
                        self._output = log_content
                        
                        # Парсим токены
                        self.token_usage = self._parse_token_usage(self._output)
                        if self.token_usage:
                            logger.info(f"[{self.WORKER_NAME}] {self.token_usage}")
                        
                        logger.info(f"[{self.WORKER_NAME}] Visible session completed")
                        return True, self._output
                except Exception as e:
                    logger.warning(f"[{self.WORKER_NAME}] Error reading log: {e}")
            
            # Проверяем жива ли сессия (fallback)
            proc = await asyncio.create_subprocess_exec(
                "tmux", "has-session", "-t", self._tmux_session,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            ret = await proc.wait()
            
            if ret != 0:
                # Сессия завершилась - читаем последний лог
                self._completed = True
                self.status = WorkerStatus.COMPLETED
                
                if hasattr(self, '_log_file') and os.path.exists(self._log_file):
                    with open(self._log_file, 'r') as f:
                        self._output = f.read()
                
                logger.info(f"[{self.WORKER_NAME}] Visible session closed")
                return True, self._output
            
            await asyncio.sleep(2)  # Проверяем каждые 2 секунды
        
        logger.warning(f"[{self.WORKER_NAME}] Timeout in visible mode after {timeout}s")
        self.status = WorkerStatus.STUCK
        return False, self._output
    
    async def stop(self) -> None:
        """Остановить copilot"""
        # Visible mode - убиваем tmux и закрываем терминал
        if self.visible and self._tmux_session:
            session_name = self._tmux_session
            
            logger.info(f"[{self.WORKER_NAME}] Killing tmux session: {session_name}")
            proc = await asyncio.create_subprocess_exec(
                "tmux", "kill-session", "-t", session_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            
            # Закрываем окно терминала по имени сессии в заголовке
            try:
                applescript = f'''
                tell application "Terminal"
                    set windowList to windows
                    repeat with w in windowList
                        try
                            if name of w contains "{session_name}" then
                                close w
                                exit repeat
                            end if
                        end try
                    end repeat
                end tell
                '''
                close_proc = await asyncio.create_subprocess_exec(
                    "osascript", "-e", applescript,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await close_proc.wait()
                logger.info(f"[{self.WORKER_NAME}] Closed Terminal window for session {session_name}")
            except Exception as e:
                logger.warning(f"[{self.WORKER_NAME}] Could not close Terminal: {e}")
            
            self._tmux_session = None
            self._terminal_window_id = None
            
            # Удаляем лог файл
            import os
            if hasattr(self, '_log_file') and self._log_file and os.path.exists(self._log_file):
                try:
                    os.remove(self._log_file)
                except Exception:
                    pass
        
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
