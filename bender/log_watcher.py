"""
Log Watcher - мониторинг и анализ логов CLI workers

Оптимизации:
- Паттерны проверяются ВСЕГДА (без LLM)
- LLM вызывается ТОЛЬКО при зависании (300s) или детекции завершения
- Tail логов (последние N строк)
"""

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Optional, Callable, Awaitable, Union
from enum import Enum

from .log_filter import LogFilter, FilteredLog
from .glm_client import GLMClient, clean_surrogates
from .llm_router import LLMRouter
from .workers.base import WorkerStatus
from .context_manager import ContextManager

logger = logging.getLogger(__name__)


class AnalysisResult(str, Enum):
    """Результат анализа логов"""
    WORKING = "working"       # Модель работает, всё ок
    COMPLETED = "completed"   # Задача выполнена
    STUCK = "stuck"           # Зависла, нужен перезапуск
    LOOP = "loop"             # Зациклилась
    NEED_HUMAN = "need_human" # Нужен человек
    ERROR = "error"           # Критическая ошибка


@dataclass
class WatcherAnalysis:
    """Результат анализа от watcher'а"""
    result: AnalysisResult
    summary: str
    suggestion: Optional[str]
    should_restart: bool = False
    context_for_restart: Optional[str] = None


class LogWatcher:
    """Наблюдатель за логами CLI workers
    
    Логика вызова LLM (экономия токенов):
    - Паттерны проверяются ВСЕГДА (бесплатно)
    - LLM вызывается ТОЛЬКО если:
      1. Лог не меняется 300 секунд (10 проверок по 30 сек)
      2. Паттерн детектировал завершение (для подтверждения)
    """
    
    # Таймаут "зависания" в секундах
    # 10 минут - достаточно для долгих задач, LLM может думать долго
    STUCK_TIMEOUT_SECONDS = 600  # 10 минут без изменений
    CHECK_INTERVAL = 30  # Интервал проверок
    
    ANALYSIS_PROMPT = """Ты наблюдатель за AI-ассистентом. Определи статус работы.

ЗАДАЧА:
{task}

ЛОГ (последние сообщения):
```
{log}
```

Время работы: {elapsed:.0f} сек

Ответь JSON:
{{
    "status": "working|completed|stuck|loop|need_human|error",
    "summary": "Что делает (1-2 предложения)",
    "suggestion": "что делать (null если ок)"
}}

Статусы:
- working: активно работает
- completed: задача ВЫПОЛНЕНА
- stuck: застряла
- loop: зациклилась
- need_human: ждёт человека
- error: ошибка

Только JSON."""

    def __init__(self, glm_client: Union[GLMClient, LLMRouter], log_filter: Optional[LogFilter] = None):
        self.glm = glm_client
        self.filter = log_filter or LogFilter()
        self.context = ContextManager()
        
        # Для детекции зависания
        self._last_log_hash: Optional[str] = None
        self._last_log_time: float = time.time()
        self._no_change_count: int = 0
    
    def _compute_hash(self, log: str) -> str:
        """Быстрый хеш лога"""
        return hashlib.md5(log.encode()[:5000]).hexdigest()
    
    async def analyze(
        self,
        raw_log: str,
        task: str,
        elapsed_seconds: float,
        process_alive: bool = True  # Процесс ещё работает?
    ) -> WatcherAnalysis:
        """Анализ лога
        
        LLM вызывается ТОЛЬКО при:
        1. Зависании (600s без изменений) И процесс НЕ активен
        2. Паттерн показал completion (для подтверждения)
        
        Args:
            raw_log: Сырой лог
            task: Текст задачи
            elapsed_seconds: Время с начала задачи
            process_alive: True если процесс (copilot/droid) ещё работает
        """
        
        # Обрезаем лог
        trimmed_log = self.context.tail_log(raw_log)
        
        logger.debug(f"[LogWatcher] Analyzing: raw_len={len(raw_log)}, elapsed={elapsed_seconds:.0f}s, process_alive={process_alive}")
        
        # Проверяем copilot completion ПЕРЕД фильтрацией (filter убирает "Type @")
        copilot_result = self._check_copilot_completion(trimmed_log)
        if copilot_result:
            logger.debug(f"[LogWatcher] Copilot completion detected")
            return copilot_result
        
        # Фильтруем шум
        filtered = self.filter.filter(trimmed_log)
        log_content = filtered.model_messages
        
        logger.debug(f"[LogWatcher] Filtered: raw={filtered.raw_length}, filtered={filtered.filtered_length}")
        
        # Слишком короткий - ждём, но обновляем время чтобы не застрять
        if filtered.filtered_length < 50:
            # Если raw лог меняется - всё ещё работаем, обновляем время
            raw_hash = hashlib.md5(raw_log[-1000:].encode() if len(raw_log) > 1000 else raw_log.encode()).hexdigest()
            if raw_hash != self._last_log_hash:
                self._last_log_hash = raw_hash
                self._last_log_time = time.time()
                self._no_change_count = 0
                logger.debug(f"[LogWatcher] Short log but raw changed - resetting timer")
            return WatcherAnalysis(
                result=AnalysisResult.WORKING,
                summary="Модель начала работу",
                suggestion=None,
            )
        
        # Проверяем изменился ли лог
        current_hash = self._compute_hash(log_content)
        log_changed = current_hash != self._last_log_hash
        
        if log_changed:
            self._last_log_hash = current_hash
            self._last_log_time = time.time()
            self._no_change_count = 0
            logger.debug(f"[LogWatcher] Log changed - resetting timer")
        else:
            self._no_change_count += 1
            stuck_seconds = time.time() - self._last_log_time
            logger.debug(f"[LogWatcher] Log unchanged for {stuck_seconds:.0f}s (count={self._no_change_count})")
        
        # 1. ВСЕГДА проверяем паттерны (бесплатно)
        pattern_result = self._analyze_by_patterns(log_content)
        
        if pattern_result:
            # Паттерн сработал!
            if pattern_result.result == AnalysisResult.COMPLETED:
                logger.info(f"[LogWatcher] Completion pattern detected: {pattern_result.summary}")
                # Для завершения - можем вызвать LLM для подтверждения, но не обязательно
                self.context.add_checkpoint(pattern_result.result.value, pattern_result.summary)
                return pattern_result
            elif pattern_result.result == AnalysisResult.ERROR:
                logger.warning(f"[LogWatcher] Error pattern: {pattern_result.summary}")
                return pattern_result
            elif pattern_result.result == AnalysisResult.NEED_HUMAN:
                return pattern_result
        
        # 2. Проверяем зависание (только если процесс НЕ активен или очень долго нет изменений)
        stuck_seconds = time.time() - self._last_log_time
        is_stuck = stuck_seconds >= self.STUCK_TIMEOUT_SECONDS
        
        # Если процесс активен - НЕ считаем это stuck, просто ждём
        # Copilot/droid могут долго думать без вывода в лог
        if process_alive and is_stuck:
            logger.info(f"[LogWatcher] No log changes for {stuck_seconds:.0f}s but process is alive - continuing to wait")
            return WatcherAnalysis(
                result=AnalysisResult.WORKING,
                summary=f"Процесс работает ({stuck_seconds:.0f}s без вывода)",
                suggestion=None,
            )
        
        if is_stuck:
            logger.warning(f"[LogWatcher] No log changes for {stuck_seconds:.0f}s and process not alive - calling LLM")
            # Вызываем LLM чтобы понять что случилось
            try:
                analysis = await self._analyze_with_glm(log_content, task, elapsed_seconds)
                self.context.add_checkpoint(analysis.result.value, analysis.summary)
                return analysis
            except Exception as e:
                logger.warning(f"LLM unavailable: {e}")
                # LLM недоступен - считаем что застряло
                return WatcherAnalysis(
                    result=AnalysisResult.STUCK,
                    summary=f"Нет изменений {stuck_seconds:.0f}s, LLM недоступен",
                    suggestion="Проверьте вручную",
                )
        
        # 3. Лог меняется - просто работает
        return WatcherAnalysis(
            result=AnalysisResult.WORKING,
            summary="Ассистент работает",
            suggestion=None,
        )
    
    def _check_copilot_completion(self, raw_log: str) -> Optional[WatcherAnalysis]:
        """Проверка завершения для Copilot (до фильтрации!)
        
        Copilot после ответа показывает:
        ● <ответ>
        ...
        Type @ to mention files
        
        Фильтр убирает "Type @" поэтому проверяем raw лог.
        """
        last_chunk = raw_log[-3000:] if len(raw_log) > 3000 else raw_log
        
        # Copilot дал ответ (●) и вернулся к prompt
        if "●" in last_chunk and "Type @ to mention" in last_chunk:
            answer_pos = last_chunk.rfind("●")
            prompt_pos = last_chunk.rfind("Type @ to mention")
            if answer_pos < prompt_pos:
                logger.info("[LogWatcher] Copilot completion: answered and returned to prompt")
                return WatcherAnalysis(
                    result=AnalysisResult.COMPLETED,
                    summary="Copilot завершил задачу",
                    suggestion=None,
                )
        return None
    
    def _analyze_by_patterns(self, log: str) -> Optional[WatcherAnalysis]:
        """Анализ по паттернам (без LLM!)"""
        last_chunk = log[-2000:] if len(log) > 2000 else log
        
        # Специальная проверка для Copilot interactive:
        # Если есть ответ (●) и после него prompt - задача завершена
        if "●" in last_chunk and "Type @ to mention" in last_chunk:
            # Проверяем что ● идёт ПЕРЕД prompt (ответ был дан)
            answer_pos = last_chunk.rfind("●")
            prompt_pos = last_chunk.rfind("Type @ to mention")
            if answer_pos < prompt_pos:
                logger.debug("[LogWatcher] Copilot answered and returned to prompt")
                return WatcherAnalysis(
                    result=AnalysisResult.COMPLETED,
                    summary="Copilot дал ответ и готов к новой задаче",
                    suggestion=None,
                )
        
        # Паттерны завершения (Copilot, Codex, Droid)
        completion_patterns = [
            # Copilot
            ("Total usage est:", "Copilot завершил работу"),
            ("API time spent:", "Сессия завершена"),
            ("Premium request", "Copilot завершил"),
            # Codex
            ("CRITICAL:", "Найдены критические проблемы"),
            ("HIGH:", "Найдены проблемы"),
            ("Проблем не найдено", "Проверка завершена"),
            ("vladimirdoronin@", "Вернулся к shell prompt"),
            # Droid
            ("Changes saved", "Droid сохранил изменения"),
            ("File updated", "Файл обновлён"),
            # Общие
            ("Task completed", "Задача завершена"),
            ("Successfully", "Успешно"),
            ("I've completed", "Завершено"),
            ("Готово", "Готово"),
        ]
        
        for pattern, summary in completion_patterns:
            if pattern in last_chunk:
                logger.debug(f"[LogWatcher] Pattern match: '{pattern}'")
                return WatcherAnalysis(
                    result=AnalysisResult.COMPLETED,
                    summary=summary,
                    suggestion=None,
                )
        
        # Паттерны ошибок
        error_patterns = [
            ("Permission denied", "Нет доступа"),
            ("rate limit", "Rate limit"),
            ("429", "API перегружен"),
            ("connection refused", "Нет соединения"),
        ]
        
        for pattern, summary in error_patterns:
            if pattern.lower() in last_chunk.lower():
                return WatcherAnalysis(
                    result=AnalysisResult.ERROR,
                    summary=summary,
                    suggestion="Проверьте логи",
                )
        
        # Паттерны вопросов
        last_bit = last_chunk[-300:]
        if "?" in last_bit or "Do you want" in last_bit:
            return WatcherAnalysis(
                result=AnalysisResult.NEED_HUMAN,
                summary="Ассистент задаёт вопрос",
                suggestion="Ответьте",
            )
        
        return None
    
    async def _analyze_with_glm(
        self,
        log: str,
        task: str,
        elapsed: float
    ) -> WatcherAnalysis:
        """Глубокий анализ через LLM (вызывается редко!)"""
        
        log = clean_surrogates(log)
        task = clean_surrogates(task)
        
        # Ограничиваем размер
        if len(log) > 3000:
            log = log[-3000:]
        
        prompt = self.ANALYSIS_PROMPT.format(task=task, log=log, elapsed=elapsed)
        
        try:
            response = await self.glm.generate_json(prompt, temperature=0.1, max_tokens=200)
            
            status_map = {
                "working": AnalysisResult.WORKING,
                "completed": AnalysisResult.COMPLETED,
                "stuck": AnalysisResult.STUCK,
                "loop": AnalysisResult.LOOP,
                "need_human": AnalysisResult.NEED_HUMAN,
                "error": AnalysisResult.ERROR,
            }
            
            result = status_map.get(response.get("status", "working"), AnalysisResult.WORKING)
            
            return WatcherAnalysis(
                result=result,
                summary=response.get("summary", "Анализ"),
                suggestion=response.get("suggestion"),
            )
            
        except Exception as e:
            logger.warning(f"GLM analysis failed: {e}")
            return WatcherAnalysis(
                result=AnalysisResult.WORKING,
                summary=f"LLM error: {e}",
                suggestion=None,
            )
    
    def reset(self):
        """Сброс состояния"""
        self._last_log_hash = None
        self._last_log_time = time.time()
        self._no_change_count = 0
        self.context.reset()
    
    def get_context_stats(self) -> dict:
        return self.context.get_stats()
