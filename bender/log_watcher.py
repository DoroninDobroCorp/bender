"""
Log Watcher - мониторинг и анализ логов CLI workers

Оптимизации контекста:
- Tail логов (последние N строк)
- Скользящее окно истории
- Компрессия при переполнении
"""

import asyncio
import logging
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
    summary: str                    # Краткое описание происходящего
    suggestion: Optional[str]       # Предложение что делать
    should_restart: bool = False    # Нужен ли перезапуск
    context_for_restart: Optional[str] = None  # Контекст для перезапуска


class LogWatcher:
    """Наблюдатель за логами CLI workers
    
    Периодически анализирует логи через GLM и определяет статус выполнения.
    """
    
    ANALYSIS_PROMPT = """Ты — опытный наблюдатель за работой AI-ассистента. Твоя роль как у тимлида, который следит за работой джуна с AI.

ЗАДАЧА которую выполняет ассистент:
{task}

{history}

ЛОГ РАБОТЫ (последние сообщения):
```
{log}
```

Время работы: {elapsed:.0f} секунд

ПРОАНАЛИЗИРУЙ лог как умный человек:
1. Что конкретно сейчас делает ассистент?
2. Есть ли прогресс? (сравни с предыдущими проверками в history)
3. Задача завершена? (ищи фразы типа "I've completed", "Готово", "Task completed", вывод findings)
4. Ассистент застрял или зациклился? (повторяет одно и то же)
5. Нужна помощь человека? (вопросы, ошибки доступа)

Ответь в формате JSON:
{{
    "status": "working|completed|stuck|loop|need_human|error",
    "summary": "Подробное описание что сейчас делает (2-3 предложения, 50-100 слов). Опиши: какой компонент создаёт, какие файлы редактирует, на каком этапе задачи находится.",
    "suggestion": "что делать дальше (null если всё ок)",
    "should_restart": false,
    "context_for_restart": null
}}

Статусы:
- working: активно работает, есть прогресс
- completed: задача ВЫПОЛНЕНА (ассистент явно сказал что закончил или вывел результат)
- stuck: застряла (нет изменений, но задача не завершена)
- loop: зациклилась (повторяет одни и те же действия)
- need_human: ждёт ответа человека или нужно решение
- error: критическая ошибка (403, 429, connection refused)

ВАЖНО: 
- Если видишь "What would you like me to do next" или список findings — это COMPLETED
- Если лог не меняется но есть финальный вывод — это COMPLETED, не STUCK
- summary должен быть ПОДРОБНЫМ и понятным человеку, опиши конкретно что делает ассистент

Ответь ТОЛЬКО JSON."""

    def __init__(self, glm_client: Union[GLMClient, LLMRouter], log_filter: Optional[LogFilter] = None):
        self.glm = glm_client
        self.filter = log_filter or LogFilter()
        self.context = ContextManager()  # NEW: управление контекстом
        self._last_log_hash: Optional[str] = None
        self._no_change_count: int = 0
    
    async def analyze(
        self,
        raw_log: str,
        task: str,
        elapsed_seconds: float
    ) -> WatcherAnalysis:
        """Проанализировать лог и определить статус
        
        Вся логика на LLM — она читает логи и решает:
        - Что сейчас происходит (человеческим языком)
        - Есть ли прогресс
        - Завершена ли задача
        - Нужно ли вмешательство человека
        """
        
        # Обрезаем лог до последних N строк
        trimmed_log = self.context.tail_log(raw_log)
        
        # Фильтруем шум
        filtered = self.filter.filter(trimmed_log)
        
        # Если лог слишком короткий — ждём больше данных
        if filtered.filtered_length < 50:
            return WatcherAnalysis(
                result=AnalysisResult.WORKING,
                summary="Модель начала работу",
                suggestion=None,
            )
        
        # Всё решает LLM — она читает логи и понимает что происходит
        analysis = await self._analyze_with_glm(filtered.model_messages, task, elapsed_seconds)
        
        # Сохраняем в историю
        self.context.add_checkpoint(analysis.result.value, analysis.summary)
        
        return analysis
    
    async def _analyze_with_glm(
        self,
        log: str,
        task: str,
        elapsed: float
    ) -> WatcherAnalysis:
        """Глубокий анализ через GLM"""
        
        # Очищаем surrogate символы
        log = clean_surrogates(log)
        task = clean_surrogates(task)
        
        # NEW: Лог уже обрезан в analyze(), но добавим защиту
        if len(log) > self.context.MAX_LOG_CHARS:
            log = log[-self.context.MAX_LOG_CHARS:]
        
        # NEW: Добавляем историю проверок для контекста
        history_context = self.context.get_history_context()
        
        prompt = self.ANALYSIS_PROMPT.format(
            task=task,
            log=log,
            elapsed=elapsed,
            history=history_context,
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
        self.context.reset()  # NEW: сброс контекста
    
    def get_context_stats(self) -> dict:
        """Получить статистику контекста"""
        return self.context.get_stats()
