"""
Task Enforcer - настаивает на завершении ТЗ

Если Droid не закончил ТЗ - генерирует сообщения для завершения.
После N неудач - эскалация к человеку.
"""

from typing import Optional, List
from dataclasses import dataclass


@dataclass
class EnforcementResult:
    """Результат enforcement"""
    should_enforce: bool
    message: str
    attempt: int
    should_escalate: bool


class TaskEnforcer:
    """Enforcer для завершения ТЗ"""
    
    ENFORCEMENT_TEMPLATES = [
        "ТЗ требует: {missing}. Заверши задачу.",
        "Ты не закончил: {missing}. Доделай и покажи результат.",
        "Задача не выполнена. Осталось: {missing}. Заверши.",
        "Покажи что именно ты изменил. Если ничего - скажи прямо.",
        "Запусти и покажи что работает. Нужен результат, не обещания."
    ]
    
    def __init__(
        self,
        max_attempts: int = 5,
        llm_router = None
    ):
        """
        Args:
            max_attempts: Максимум попыток настаивания до эскалации
            llm_router: LLMRouter для генерации сообщений (опционально)
        """
        self.max_attempts = max_attempts
        self.llm = llm_router
        self._current_attempt = 0
    
    def enforce(
        self,
        missing_items: List[str],
        step_prompt: str,
        droid_response: str
    ) -> EnforcementResult:
        """Сгенерировать enforcement сообщение
        
        Args:
            missing_items: Что не сделано
            step_prompt: Оригинальное ТЗ
            droid_response: Последний ответ Droid
        
        Returns:
            EnforcementResult
        """
        self._current_attempt += 1
        
        # Проверить лимит
        if self._current_attempt >= self.max_attempts:
            return EnforcementResult(
                should_enforce=False,
                message="",
                attempt=self._current_attempt,
                should_escalate=True
            )
        
        # Выбрать шаблон
        template_idx = min(self._current_attempt - 1, len(self.ENFORCEMENT_TEMPLATES) - 1)
        template = self.ENFORCEMENT_TEMPLATES[template_idx]
        
        # Сформировать missing
        missing_text = ", ".join(missing_items) if missing_items else "завершить задачу"
        
        message = template.format(missing=missing_text)
        
        return EnforcementResult(
            should_enforce=True,
            message=message,
            attempt=self._current_attempt,
            should_escalate=False
        )
    
    async def enforce_with_llm(
        self,
        missing_items: List[str],
        step_prompt: str,
        droid_response: str,
        issues: List[str] = None
    ) -> EnforcementResult:
        """Сгенерировать enforcement через LLM
        
        Более умное сообщение с учетом контекста.
        """
        if not self.llm:
            return self.enforce(missing_items, step_prompt, droid_response)
        
        self._current_attempt += 1
        
        if self._current_attempt >= self.max_attempts:
            return EnforcementResult(
                should_enforce=False,
                message="",
                attempt=self._current_attempt,
                should_escalate=True
            )
        
        prompt = f"""Ты - Bender, следишь за Droid. Он не закончил задачу.

ТЗ ШАГА:
{step_prompt[:1000]}

ЧТО НЕ СДЕЛАНО:
{chr(10).join(f"- {m}" for m in missing_items) if missing_items else "Не указано"}

ПРОБЛЕМЫ:
{chr(10).join(f"- {i}" for i in issues) if issues else "Нет"}

ОТВЕТ DROID:
{droid_response[-500:]}

ПОПЫТКА: {self._current_attempt}/{self.max_attempts}

Напиши КОРОТКОЕ (1-2 предложения) сообщение для Droid чтобы он закончил задачу.
Будь конкретным. Не повторяй всё ТЗ - укажи что именно не сделано.
"""
        
        try:
            message = await self.llm.generate(prompt, temperature=0.5)
            # Обрезать если слишком длинное
            if len(message) > 300:
                message = message[:300] + "..."
        except Exception:
            # Fallback на шаблон
            return self.enforce(missing_items, step_prompt, droid_response)
        
        return EnforcementResult(
            should_enforce=True,
            message=message.strip(),
            attempt=self._current_attempt,
            should_escalate=False
        )
    
    def reset(self):
        """Сбросить счетчик попыток"""
        self._current_attempt = 0
    
    @property
    def attempts(self) -> int:
        """Текущее количество попыток"""
        return self._current_attempt
