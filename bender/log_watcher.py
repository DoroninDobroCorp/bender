"""
Log Watcher - мониторинг и анализ логов CLI workers
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional, Callable, Awaitable
from enum import Enum

from .log_filter import LogFilter, FilteredLog
from .glm_client import GLMClient
from .workers.base import WorkerStatus

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
    summary: str                    # Краткое описание происходящего
    suggestion: Optional[str]       # Предложение что делать
    should_restart: bool = False    # Нужен ли перезапуск
    context_for_restart: Optional[str] = None  # Контекст для перезапуска


class LogWatcher:
    """Наблюдатель за логами CLI workers
    
    Периодически анализирует логи через GLM и определяет статус выполнения.
    """
    
    ANALYSIS_PROMPT = """Ты анализируешь лог работы AI-ассистента над задачей.
Твоя задача - определить текущий статус выполнения.

ЗАДАЧА: {task}

ЛОГ РАБОТЫ (только сообщения модели, без вывода команд):
```
{log}
```

Время работы: {elapsed:.0f} секунд

Определи статус и ответь в формате JSON:
{{
    "status": "working|completed|stuck|loop|need_human|error",
    "summary": "краткое описание что происходит (1-2 предложения)",
    "suggestion": "что делать дальше (null если working)",
    "should_restart": false,
    "context_for_restart": null
}}

Статусы:
- working: модель активно работает над задачей
- completed: задача выполнена успешно
- stuck: модель зависла (нет прогресса >2 минут, повторяет одно и то же)
- loop: модель зациклилась (делает одно и то же действие снова и снова)
- need_human: модель просит помощи человека или нужно решение человека
- error: произошла критическая ошибка

Если should_restart=true, в context_for_restart напиши что было сделано для передачи в новую сессию.

Ответь ТОЛЬКО JSON, без комментариев."""

    def __init__(self, glm_client: GLMClient, log_filter: Optional[LogFilter] = None):
        self.glm = glm_client
        self.filter = log_filter or LogFilter()
        self._last_log_hash: Optional[str] = None
        self._no_change_count: int = 0
    
    async def analyze(
        self,
        raw_log: str,
        task: str,
        elapsed_seconds: float
    ) -> WatcherAnalysis:
        """Проанализировать лог и определить статус"""
        
        # Фильтруем лог
        filtered = self.filter.filter(raw_log)
        
        # Быстрые проверки без GLM
        if filtered.has_completion and not filtered.has_error:
            return WatcherAnalysis(
                result=AnalysisResult.COMPLETED,
                summary="Задача выполнена успешно",
                suggestion=None,
            )
        
        if filtered.has_question:
            return WatcherAnalysis(
                result=AnalysisResult.NEED_HUMAN,
                summary="Модель задаёт вопрос",
                suggestion="Проверьте вопрос и ответьте",
            )
        
        # Проверка на зависание (нет изменений в логе)
        current_hash = hash(filtered.model_messages)
        if current_hash == self._last_log_hash:
            self._no_change_count += 1
            if self._no_change_count >= 3:  # 3 проверки без изменений
                return WatcherAnalysis(
                    result=AnalysisResult.STUCK,
                    summary="Нет прогресса в логах",
                    suggestion="Перезапустить с контекстом",
                    should_restart=True,
                    context_for_restart=self._extract_context(filtered.model_messages),
                )
        else:
            self._no_change_count = 0
        self._last_log_hash = current_hash
        
        # Если лог слишком короткий, считаем что работа идёт
        if filtered.filtered_length < 100:
            return WatcherAnalysis(
                result=AnalysisResult.WORKING,
                summary="Модель начала работу",
                suggestion=None,
            )
        
        # Полный анализ через GLM
        return await self._analyze_with_glm(filtered.model_messages, task, elapsed_seconds)
    
    async def _analyze_with_glm(
        self,
        log: str,
        task: str,
        elapsed: float
    ) -> WatcherAnalysis:
        """Глубокий анализ через GLM"""
        
        # Ограничить длину лога для GLM
        if len(log) > 4000:
            log = log[-4000:]  # Последние 4000 символов
        
        prompt = self.ANALYSIS_PROMPT.format(
            task=task,
            log=log,
            elapsed=elapsed,
        )
        
        try:
            response = await self.glm.generate_json(prompt, temperature=0.1)
            
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
                summary=response.get("summary", "Анализ недоступен"),
                suggestion=response.get("suggestion"),
                should_restart=response.get("should_restart", False),
                context_for_restart=response.get("context_for_restart"),
            )
            
        except Exception as e:
            logger.warning(f"GLM analysis failed: {e}")
            return WatcherAnalysis(
                result=AnalysisResult.WORKING,
                summary="Не удалось проанализировать (GLM error)",
                suggestion=None,
            )
    
    def _extract_context(self, log: str, max_length: int = 500) -> str:
        """Извлечь контекст для перезапуска"""
        lines = log.strip().split('\n')
        
        # Берём последние строки
        context_lines = []
        current_length = 0
        
        for line in reversed(lines):
            if current_length + len(line) > max_length:
                break
            context_lines.insert(0, line)
            current_length += len(line) + 1
        
        return '\n'.join(context_lines)
    
    def reset(self):
        """Сбросить состояние watcher'а"""
        self._last_log_hash = None
        self._no_change_count = 0
