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
from typing import Optional, List, Callable, Awaitable
from dataclasses import dataclass
from enum import Enum


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
    
    def __init__(
        self,
        check_interval: int = 300,      # 5 минут
        stuck_threshold: int = 3600,    # 1 час (12 проверок)
        loop_threshold: int = 3,        # 3 одинаковых сообщения
        error_patterns: List[str] = None
    ):
        self.check_interval = check_interval
        self.stuck_threshold = stuck_threshold
        self.loop_threshold = loop_threshold
        self.error_patterns = error_patterns or [
            r'Exception',
            r'Error:',
            r'FAILED',
            r'Traceback',
            r'panic:',
            r'fatal:'
        ]
        
        # Состояние
        self._last_output: str = ""
        self._last_output_time: float = time.time()
        self._output_history: List[str] = []
        self._stuck_checks: int = 0
        self._running: bool = False
    
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
            if re.search(pattern, current_output, re.IGNORECASE):
                return HealthCheck(
                    status=HealthStatus.ERROR,
                    action=WatchdogAction.NEW_CHAT,
                    reason=f"Error detected: {pattern}",
                    details=self._extract_error_context(current_output, pattern)
                )
        
        # 3. Проверка зацикливания
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
    
    def _is_looping(self, current_output: str) -> bool:
        """Проверить зацикливание"""
        # Добавить в историю (последние 500 символов для сравнения)
        output_hash = current_output[-500:] if len(current_output) > 500 else current_output
        self._output_history.append(output_hash)
        
        # Оставить только последние N записей
        if len(self._output_history) > self.loop_threshold + 2:
            self._output_history = self._output_history[-(self.loop_threshold + 2):]
        
        # Проверить повторения
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
    
    async def start_monitoring(
        self,
        get_output: Callable[[], str],
        is_alive: Callable[[], bool],
        on_issue: Callable[[HealthCheck], Awaitable[None]]
    ):
        """Запустить фоновый мониторинг
        
        Args:
            get_output: Функция получения текущего output
            is_alive: Функция проверки жива ли сессия
            on_issue: Callback при обнаружении проблемы
        """
        self._running = True
        self.reset()
        
        while self._running:
            await asyncio.sleep(self.check_interval)
            
            if not self._running:
                break
            
            check = self.check_health(get_output(), is_alive())
            
            if check.action != WatchdogAction.NONE:
                await on_issue(check)
    
    def stop_monitoring(self):
        """Остановить мониторинг"""
        self._running = False
