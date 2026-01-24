"""
Context Manager - управление контекстом GLM для предотвращения переполнения

Стратегии:
1. Tail логов - читать только последние N строк
2. Скользящее окно - хранить последние K проверок
3. Компрессия - сжимать историю при >75% контекста
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class CheckpointSummary:
    """Сжатый summary одной проверки"""
    timestamp: datetime
    status: str
    summary: str
    
    def __str__(self) -> str:
        return f"[{self.status}] {self.summary}"


@dataclass
class ContextBudget:
    """Бюджет контекста"""
    max_tokens: int = 100_000  # GLM ~128k, оставляем запас
    current_tokens: int = 0
    warning_threshold: float = 0.75  # 75% - начинаем компрессию
    
    @property
    def usage_percent(self) -> float:
        return self.current_tokens / self.max_tokens
    
    @property
    def needs_compression(self) -> bool:
        return self.usage_percent >= self.warning_threshold
    
    def estimate_tokens(self, text: str) -> int:
        """Примерная оценка токенов (1 токен ≈ 4 символа для английского)"""
        # Для русского/смешанного текста более консервативная оценка
        return len(text) // 3


class ContextManager:
    """Менеджер контекста для GLM
    
    Следит за размером контекста и сжимает когда нужно.
    """
    
    # Лимиты
    MAX_LOG_LINES = 50           # Максимум строк лога за раз
    MAX_LOG_CHARS = 4000         # Максимум символов лога
    MAX_HISTORY_ITEMS = 5        # Максимум проверок в истории
    COMPRESSION_SUMMARY_LEN = 200  # Длина сжатого summary
    
    def __init__(self, max_tokens: int = 100_000):
        self.budget = ContextBudget(max_tokens=max_tokens)
        self.history: List[CheckpointSummary] = []
        self._full_history: List[CheckpointSummary] = []  # Для отладки
        self._compression_count = 0
    
    def tail_log(self, raw_log: str, max_lines: int = None, max_chars: int = None) -> str:
        """Взять только хвост лога
        
        Берём последние N строк или последние M символов (что меньше).
        """
        max_lines = max_lines or self.MAX_LOG_LINES
        max_chars = max_chars or self.MAX_LOG_CHARS
        
        if not raw_log:
            return ""
        
        # Сначала обрезаем по символам
        if len(raw_log) > max_chars:
            raw_log = raw_log[-max_chars:]
            # Найти начало первой полной строки
            newline_pos = raw_log.find('\n')
            if newline_pos > 0:
                raw_log = raw_log[newline_pos + 1:]
        
        # Потом по строкам
        lines = raw_log.strip().split('\n')
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
        
        return '\n'.join(lines)
    
    def add_checkpoint(self, status: str, summary: str) -> None:
        """Добавить результат проверки в историю"""
        checkpoint = CheckpointSummary(
            timestamp=datetime.now(),
            status=status,
            summary=summary[:self.COMPRESSION_SUMMARY_LEN],
        )
        
        self.history.append(checkpoint)
        self._full_history.append(checkpoint)
        
        # Обновить бюджет
        self.budget.current_tokens += self.budget.estimate_tokens(str(checkpoint))
        
        # Сжать если надо
        if len(self.history) > self.MAX_HISTORY_ITEMS:
            self._compress_history()
        
        if self.budget.needs_compression:
            logger.warning(
                f"Context budget at {self.budget.usage_percent:.0%}, "
                f"compressing history..."
            )
            self._compress_history()
    
    def _compress_history(self) -> None:
        """Сжать историю, оставив только последние N записей"""
        if len(self.history) <= 2:
            return
        
        old_count = len(self.history)
        
        # Оставляем первую (начало) и последние (актуальные)
        keep_count = min(self.MAX_HISTORY_ITEMS, 3)
        self.history = [self.history[0]] + self.history[-(keep_count - 1):]
        
        # Пересчитать бюджет
        self.budget.current_tokens = sum(
            self.budget.estimate_tokens(str(h)) for h in self.history
        )
        
        self._compression_count += 1
        logger.info(
            f"Compressed history: {old_count} → {len(self.history)} items "
            f"(compression #{self._compression_count})"
        )
    
    def get_history_context(self) -> str:
        """Получить историю для контекста GLM"""
        if not self.history:
            return "Нет предыдущих проверок."
        
        lines = ["Предыдущие проверки:"]
        for h in self.history:
            time_str = h.timestamp.strftime("%H:%M:%S")
            lines.append(f"  [{time_str}] {h}")
        
        return '\n'.join(lines)
    
    def reset(self) -> None:
        """Сбросить состояние для новой задачи"""
        self.history = []
        self.budget.current_tokens = 0
        logger.debug(
            f"Context reset. Total compressions in session: {self._compression_count}"
        )
    
    def get_stats(self) -> dict:
        """Получить статистику контекста"""
        return {
            "history_size": len(self.history),
            "full_history_size": len(self._full_history),
            "tokens_used": self.budget.current_tokens,
            "tokens_max": self.budget.max_tokens,
            "usage_percent": f"{self.budget.usage_percent:.1%}",
            "compressions": self._compression_count,
        }
