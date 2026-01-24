"""
Log Filter - фильтрация логов CLI инструментов

Отфильтровывает вывод команд, оставляя только сообщения и размышления модели.
"""

import re
import logging
from typing import List, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class FilteredLog:
    """Отфильтрованный лог"""
    model_messages: str      # Только сообщения модели
    has_completion: bool     # Есть ли признак завершения
    has_error: bool          # Есть ли ошибка
    has_question: bool       # Модель задаёт вопрос
    raw_length: int          # Длина исходного лога
    filtered_length: int     # Длина после фильтрации


class LogFilter:
    """Фильтр логов CLI инструментов
    
    Извлекает только сообщения и размышления модели,
    отбрасывая вывод команд (npm install, git status, etc).
    """
    
    # Паттерны для сообщений модели (то что ОСТАВЛЯЕМ)
    MODEL_PATTERNS = [
        # Copilot / Claude
        r"^\[Claude\].*",
        r"^\[Model\].*",
        r"^Thinking:.*",
        r"^> .*",  # Markdown цитаты часто используются для мыслей
        r"^I('m| am| will| can|'ll).*",  # Начало предложений от модели
        r"^Let me.*",
        r"^Now I.*",
        r"^First,.*",
        r"^Next,.*",
        r"^Finally,.*",
        r"^Looking at.*",
        r"^Analyzing.*",
        r"^The (error|issue|problem|solution).*",
        r"^This (is|looks|seems|appears).*",
        r"^I (see|found|notice|think|believe).*",
        r"^Based on.*",
        r"^According to.*",
        
        # Copilot specific
        r"^●.*",  # Copilot bullet points
        r"^✓.*",
        r"^✗.*",
        r"^→.*",
        
        # Codex specific  
        r"^\[codex\].*",
        r"^Plan:.*",
        r"^Step \d+:.*",
        
        # Droid specific
        r"^\[droid\].*",
        r"^Assistant:.*",
    ]
    
    # Паттерны для вывода команд (то что ОТБРАСЫВАЕМ)
    COMMAND_PATTERNS = [
        r"^\$\s+.*",           # $ command
        r"^>\s+.*",            # > command (Windows/npm)
        r"^\+\s+.*",           # + added package
        r"^npm\s+(WARN|ERR|info).*",
        r"^added \d+ packages.*",
        r"^up to date.*",
        r"^\d+ packages are looking.*",
        r"^Run `npm.*",
        r"^diff --git.*",
        r"^index [a-f0-9]+\.\.[a-f0-9]+.*",
        r"^@@.*@@.*",
        r"^[-+]{3}\s+[ab]/.*",  # --- a/file, +++ b/file
        r"^[+-]\s+.*",          # Diff lines
        r"^\s*\d+\s+passing.*",  # Test results
        r"^\s*\d+\s+failing.*",
        r"^PASS\s+.*",
        r"^FAIL\s+.*",
        r"^✔.*test.*",
        r"^✖.*test.*",
        r"^Compiling.*",
        r"^Building.*",
        r"^Bundling.*",
        r"^warning:.*",          # Compiler warnings
        r"^error\[E\d+\]:.*",    # Rust errors
        r"^  --> .*:\d+:\d+.*",  # Rust/compiler locations
        r"^\s+\|.*",             # Rust error context
        r"^node_modules/.*",
        r"^\s+at\s+.*\(.*:\d+:\d+\).*",  # Stack traces
        r"^.*\.js:\d+$",
        r"^.*\.ts:\d+$",
        r"^.*\.py:\d+$",
    ]
    
    # Паттерны завершения задачи
    COMPLETION_PATTERNS = [
        r"task.*complet",
        r"done!",
        r"finished",
        r"successfully",
        r"all tests pass",
        r"build succeeded",
        r"готово",
        r"выполнено",
        r"завершено",
    ]
    
    # Паттерны ошибок
    ERROR_PATTERNS = [
        r"error:",
        r"failed",
        r"exception",
        r"cannot",
        r"unable to",
        r"not found",
        r"ошибка",
        r"не удалось",
    ]
    
    # Паттерны вопросов от модели
    QUESTION_PATTERNS = [
        r"\?$",
        r"should I",
        r"do you want",
        r"would you like",
        r"can you",
        r"please (confirm|specify|clarify)",
        r"хотите",
        r"нужно ли",
        r"подтвердите",
    ]
    
    def __init__(self):
        self._model_re = [re.compile(p, re.IGNORECASE) for p in self.MODEL_PATTERNS]
        self._command_re = [re.compile(p, re.IGNORECASE) for p in self.COMMAND_PATTERNS]
        self._completion_re = [re.compile(p, re.IGNORECASE) for p in self.COMPLETION_PATTERNS]
        self._error_re = [re.compile(p, re.IGNORECASE) for p in self.ERROR_PATTERNS]
        self._question_re = [re.compile(p, re.IGNORECASE) for p in self.QUESTION_PATTERNS]
    
    def filter(self, raw_log: str) -> FilteredLog:
        """Отфильтровать лог"""
        lines = raw_log.split('\n')
        filtered_lines: List[str] = []
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Пропустить вывод команд
            if self._is_command_output(line):
                continue
            
            # Оставить сообщения модели
            if self._is_model_message(line):
                filtered_lines.append(line)
                continue
            
            # Для остальных строк - эвристика
            # Если строка длинная и похожа на текст (не код) - оставить
            if len(line) > 50 and self._looks_like_text(line):
                filtered_lines.append(line)
        
        filtered_text = '\n'.join(filtered_lines)
        
        return FilteredLog(
            model_messages=filtered_text,
            has_completion=self._check_patterns(filtered_text, self._completion_re),
            has_error=self._check_patterns(filtered_text, self._error_re),
            has_question=self._check_patterns(filtered_text, self._question_re),
            raw_length=len(raw_log),
            filtered_length=len(filtered_text),
        )
    
    def _is_command_output(self, line: str) -> bool:
        """Проверить, является ли строка выводом команды"""
        return any(pattern.match(line) for pattern in self._command_re)
    
    def _is_model_message(self, line: str) -> bool:
        """Проверить, является ли строка сообщением модели"""
        return any(pattern.match(line) for pattern in self._model_re)
    
    def _looks_like_text(self, line: str) -> bool:
        """Эвристика: похожа ли строка на текст (не код)"""
        # Много пробелов в начале = скорее код
        if line.startswith('    ') or line.startswith('\t\t'):
            return False
        
        # Много спецсимволов = скорее код
        special_chars = sum(1 for c in line if c in '{}[]();=<>|&')
        if special_chars > len(line) * 0.2:
            return False
        
        # Слова через пробелы = скорее текст
        words = line.split()
        if len(words) >= 5:
            return True
        
        return False
    
    def _check_patterns(self, text: str, patterns: List[re.Pattern]) -> bool:
        """Проверить наличие паттернов в тексте"""
        text_lower = text.lower()
        return any(pattern.search(text_lower) for pattern in patterns)
