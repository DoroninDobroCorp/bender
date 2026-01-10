"""
Watchdog - мониторинг здоровья Droid

Детектирует:
- Зависание (нет output N секунд)
- Зацикливание (одинаковые сообщения 3+ раз)
- Вылет (процесс tmux умер)
- Ошибки (Exception/Error в логах)
"""

import asyncio
import time
import re
import logging
from typing import Optional, List, Callable, Awaitable
from dataclasses import dataclass
from enum import Enum


logger = logging.getLogger(__name__)


class HealthStatus(str, Enum):
    """Статус здоровья Droid"""
    HEALTHY = "HEALTHY"
    STUCK = "STUCK"           # Завис
    LOOPING = "LOOPING"       # Зациклился
    CRASHED = "CRASHED"       # Вылетел
    ERROR = "ERROR"           # Ошибка в output


class WatchdogAction(str, Enum):
    """Действия watchdog"""
    NONE = "NONE"             # Все ок
    WAIT = "WAIT"             # Подождать еще
    PING = "PING"             # Отправить Enter
    RESTART = "RESTART"       # Перезапустить Droid
    NEW_CHAT = "NEW_CHAT"     # Открыть новый чат
    ESCALATE = "ESCALATE"     # Эскалация к человеку


@dataclass
class HealthCheck:
    """Результат проверки здоровья"""
    status: HealthStatus
    action: WatchdogAction
    reason: str
    details: Optional[str] = None


class Watchdog:
    """Watchdog для мониторинга Droid"""
    
    # Error patterns that indicate real problems (more specific to avoid false positives)
    DEFAULT_ERROR_PATTERNS = [
        r'^Traceback \(most recent call last\):',  # Python traceback start
        r'\bpanic:\s',  # Go panic
        r'^Unhandled\s+exception\s+in',  # Unhandled exception
        r'^CRITICAL:\s',  # Critical log level
        r'\bSegmentation fault\b',
        r'\bSIGKILL\b',  # Signal kill
        r'\bSIGSEGV\b',  # Segmentation fault signal
        r'^Out of memory\b',
        r'^MemoryError\b',  # Python memory error
        r'^\s*Process\s+killed\s+by\s+signal',  # Process killed by signal
    ]
    
    # Patterns that look like errors but are often normal operation
    DEFAULT_FALSE_POSITIVE_PATTERNS = [
        r'fatal: not a git repository',  # Normal git check
        r'fatal: ambiguous argument',  # Git diff on new files
        r'error: pathspec',  # Git checkout non-existent
        r'npm WARN',  # npm warnings
        r'warning:',  # General warnings
        r'Error: ENOENT',  # File not found (often expected)
        r'killed\s+successfully',  # Intentional kill
        r'process\s+exited\s+with\s+code\s+0',  # Normal exit
    ]
    
    def __init__(
        self,
        check_interval: int = 300,      # 5 минут
        stuck_threshold: int = 3600,    # 1 час (12 проверок)
        loop_threshold: int = 3,        # 3 одинаковых сообщения
        error_patterns: Optional[List[str]] = None,
        false_positive_patterns: Optional[List[str]] = None,
        max_consecutive_errors: int = 3,
        error_backoff_multiplier: float = 2.0
    ):
        self.check_interval = check_interval
        self.stuck_threshold = stuck_threshold
        self.loop_threshold = loop_threshold
        self.error_patterns = error_patterns or self.DEFAULT_ERROR_PATTERNS
        self.false_positive_patterns = false_positive_patterns or self.DEFAULT_FALSE_POSITIVE_PATTERNS
        self.max_consecutive_errors = max_consecutive_errors
        self.error_backoff_multiplier = error_backoff_multiplier
        
        # Состояние
        self._last_output: str = ""
        self._last_output_time: float = time.time()
        self._output_history: List[str] = []
        self._stuck_checks: int = 0
        self._running: bool = False
        self._cleanup_callback: Optional[Callable[[], Awaitable[None]]] = None
        self._consecutive_errors: int = 0
        self._current_backoff: float = 0.0
    
    def check_health(
        self,
        current_output: str,
        is_session_alive: bool
    ) -> HealthCheck:
        """Проверить здоровье Droid
        
        Args:
            current_output: Текущий output из tmux
            is_session_alive: Жива ли tmux сессия
        
        Returns:
            HealthCheck с результатом
        """
        # 1. Проверка вылета
        if not is_session_alive:
            return HealthCheck(
                status=HealthStatus.CRASHED,
                action=WatchdogAction.RESTART,
                reason="tmux session is dead"
            )
        
        # 2. Проверка ошибок в output
        for pattern in self.error_patterns:
            try:
                if re.search(pattern, current_output, re.IGNORECASE | re.MULTILINE):
                    # Check if it's a false positive
                    is_false_positive = any(
                        re.search(fp, current_output, re.IGNORECASE)
                        for fp in self.false_positive_patterns
                    )
                    if not is_false_positive:
                        return HealthCheck(
                            status=HealthStatus.ERROR,
                            action=WatchdogAction.NEW_CHAT,
                            reason=f"Error detected: {pattern}",
                            details=self._extract_error_context(current_output, pattern)
                        )
            except re.error as e:
                logger.warning(f"Invalid regex pattern '{pattern}': {e}")
        
        # 3. Проверка зацикливания (update history first, then check)
        self._update_looping_history(current_output)
        if self._is_looping(current_output):
            return HealthCheck(
                status=HealthStatus.LOOPING,
                action=WatchdogAction.NEW_CHAT,
                reason=f"Same output {self.loop_threshold}+ times"
            )
        
        # 4. Проверка зависания
        if current_output != self._last_output:
            # Output изменился - сбросить счетчик
            self._last_output = current_output
            self._last_output_time = time.time()
            self._stuck_checks = 0
        else:
            # Output не изменился
            self._stuck_checks += 1
            stuck_time = time.time() - self._last_output_time
            
            if stuck_time >= self.stuck_threshold:
                return HealthCheck(
                    status=HealthStatus.STUCK,
                    action=WatchdogAction.ESCALATE,
                    reason=f"No output for {stuck_time/60:.0f} minutes"
                )
            elif self._stuck_checks >= 3:
                # 3 проверки без изменений - попробовать пинг
                return HealthCheck(
                    status=HealthStatus.STUCK,
                    action=WatchdogAction.PING,
                    reason=f"No output for {self._stuck_checks} checks"
                )
        
        # Все ок
        return HealthCheck(
            status=HealthStatus.HEALTHY,
            action=WatchdogAction.NONE,
            reason="Droid is healthy"
        )
    
    def _update_looping_history(self, current_output: str):
        """Update looping history only when output changes"""
        output_hash = current_output[-500:] if len(current_output) > 500 else current_output
        self._output_history.append(output_hash)
        
        # Keep only last N+2 entries
        if len(self._output_history) > self.loop_threshold + 2:
            self._output_history = self._output_history[-(self.loop_threshold + 2):]
    
    def _is_looping(self, current_output: str) -> bool:
        """Проверить зацикливание (checks existing history, doesn't add)"""
        # Check if last N outputs are identical
        if len(self._output_history) >= self.loop_threshold:
            last_outputs = self._output_history[-self.loop_threshold:]
            if len(set(last_outputs)) == 1:
                return True
        
        return False
    
    def _extract_error_context(self, output: str, pattern: str) -> str:
        """Извлечь контекст ошибки"""
        lines = output.split('\n')
        for i, line in enumerate(lines):
            if re.search(pattern, line, re.IGNORECASE):
                # Вернуть 3 строки до и после
                start = max(0, i - 3)
                end = min(len(lines), i + 4)
                return '\n'.join(lines[start:end])
        return ""
    
    def reset(self):
        """Сбросить состояние"""
        self._last_output = ""
        self._last_output_time = time.time()
        self._output_history = []
        self._stuck_checks = 0
        self._consecutive_errors = 0
        self._current_backoff = 0.0
    
    async def start_monitoring(
        self,
        get_output: Callable[[], str],
        is_alive: Callable[[], bool],
        on_issue: Callable[[HealthCheck], Awaitable[None]],
        on_cleanup: Optional[Callable[[], Awaitable[None]]] = None
    ):
        """Запустить фоновый мониторинг
        
        Args:
            get_output: Функция получения текущего output
            is_alive: Функция проверки жива ли сессия
            on_issue: Callback при обнаружении проблемы
            on_cleanup: Optional cleanup callback on shutdown
        """
        self._running = True
        self._cleanup_callback = on_cleanup
        self.reset()
        
        try:
            while self._running:
                # Apply backoff delay if we had consecutive errors
                sleep_time = self.check_interval + self._current_backoff
                try:
                    await asyncio.sleep(sleep_time)
                except asyncio.CancelledError:
                    logger.debug("Watchdog monitoring cancelled")
                    break
                
                if not self._running:
                    break
                
                try:
                    check = self.check_health(get_output(), is_alive())
                    
                    if check.action != WatchdogAction.NONE:
                        await on_issue(check)
                    
                    # Reset error state on successful check
                    self._consecutive_errors = 0
                    self._current_backoff = 0.0
                    
                except Exception as e:
                    self._consecutive_errors += 1
                    logger.warning(f"Watchdog check error ({self._consecutive_errors}/{self.max_consecutive_errors}): {e}")
                    
                    # Apply exponential backoff
                    if self._consecutive_errors >= self.max_consecutive_errors:
                        self._current_backoff = min(
                            self._current_backoff * self.error_backoff_multiplier if self._current_backoff > 0 else self.check_interval,
                            self.check_interval * 10  # Max 10x normal interval
                        )
                        logger.warning(f"Watchdog backoff increased to {self._current_backoff:.1f}s")
        finally:
            # Cleanup on exit
            self.reset()
            if self._cleanup_callback:
                try:
                    await self._cleanup_callback()
                except Exception as e:
                    logger.warning(f"Watchdog cleanup error: {e}")
    
    def stop_monitoring(self):
        """Остановить мониторинг"""
        self._running = False
        logger.debug("Watchdog stop requested")
