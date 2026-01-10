"""
Response Analyzer - анализ ответов Droid через Gemini

Определяет:
- Выполнено ли ТЗ шага
- Были ли изменения
- Существенные или косметические
"""

import json
import re
import logging
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from enum import Enum

from .llm_router import LLMRouter


logger = logging.getLogger(__name__)


class AnalysisAction(str, Enum):
    """Действия после анализа"""
    CONTINUE = "CONTINUE"      # Продолжить (confirmations++)
    NEW_CHAT = "NEW_CHAT"      # Новый чат (были существенные изменения)
    ASK_DROID = "ASK_DROID"    # Спросить Droid уточнение
    ENFORCE_TASK = "ENFORCE_TASK"  # Настоять на завершении ТЗ
    ESCALATE = "ESCALATE"      # Эскалация к человеку


@dataclass
class AnalysisResult:
    """Результат анализа ответа Droid"""
    task_complete: bool
    has_changes: bool
    changes_substantial: bool
    changes_description: str
    issues: list
    action: AnalysisAction
    message_to_droid: str
    reason: str
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AnalysisResult":
        return cls(
            task_complete=data.get("task_complete", False),
            has_changes=data.get("has_changes", False),
            changes_substantial=data.get("changes_substantial", False),
            changes_description=data.get("changes_description", ""),
            issues=data.get("issues", []),
            action=AnalysisAction(data.get("action", "CONTINUE")),
            message_to_droid=data.get("message_to_droid", ""),
            reason=data.get("reason", "")
        )


class ResponseAnalyzer:
    """Анализатор ответов Droid через Gemini"""
    
    SYSTEM_PROMPT = """Ты - Bender, программист который следит за работой Droid (AI-кодера).

ТВОЯ РОЛЬ:
- Понимать ответы Droid: сделал изменения или нет
- Определять выполнено ли ТЗ шага
- Решать что делать дальше

ПРАВИЛА ОПРЕДЕЛЕНИЯ ИЗМЕНЕНИЙ:
1. Существенные изменения (has_changes=true, changes_substantial=true):
   - Новый код, новые файлы
   - Изменение логики, алгоритмов
   - Исправление багов
   - Добавление/удаление функционала
2. Несущественные изменения (has_changes=true, changes_substantial=false):
   - Typo, formatting, whitespace
   - Только комментарии (без кода)
   - Переименование без изменения логики
3. Нет изменений (has_changes=false):
   - "Всё работает", "Already correct", "No changes needed"
   - Droid только проверил и подтвердил

ПРАВИЛА ДЕЙСТВИЙ:
1. task_complete=true + has_changes=false → action="CONTINUE" (confirmations++)
2. task_complete=true + changes_substantial=true → action="NEW_CHAT" (git commit, новый чат)
3. task_complete=true + changes_substantial=false → action="CONTINUE" (без нового чата)
4. task_complete=false → action="ENFORCE_TASK"
5. Непонятно сделал ли изменения → action="ASK_DROID"
6. failed_attempts >= 5 → action="ESCALATE"
"""
    
    def __init__(
        self,
        llm_router: LLMRouter,
        truncate_length: int = 3000,
        truncate_start_ratio: float = 0.4
    ):
        """
        Args:
            llm_router: LLMRouter для вызова Gemini/GLM
            truncate_length: Max length for truncated text
            truncate_start_ratio: Ratio of text to keep from start (rest from end)
        """
        self.llm = llm_router
        self.truncate_length = truncate_length
        self.truncate_start_ratio = truncate_start_ratio
    
    def _smart_truncate(self, text: str, max_len: Optional[int] = None) -> str:
        """Smart truncation preserving start and end of text
        
        Preserves JSON blocks if they exist in the text.
        """
        if max_len is None:
            max_len = self.truncate_length
            
        if len(text) <= max_len:
            return text
        
        # Check if there's a JSON block we should preserve
        json_match = re.search(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
        if json_match:
            json_block = json_match.group(0)
            if len(json_block) < max_len * 0.5:
                # Preserve JSON block and truncate the rest
                remaining = max_len - len(json_block) - 50
                if remaining > 100:
                    start_len = int(remaining * self.truncate_start_ratio)
                    end_len = remaining - start_len
                    
                    # Find where JSON block is
                    json_start = json_match.start()
                    json_end = json_match.end()
                    
                    before = text[:json_start]
                    after = text[json_end:]
                    
                    if len(before) > start_len:
                        before = before[:start_len] + "\n... [truncated] ...\n"
                    if len(after) > end_len:
                        after = "\n... [truncated] ...\n" + after[-end_len:]
                    
                    return before + json_block + after
        
        # Standard truncation: keep start_ratio from start, rest from end
        start_len = int(max_len * self.truncate_start_ratio)
        end_len = max_len - start_len - 50  # 50 chars for separator
        
        return f"{text[:start_len]}\n\n... [truncated {len(text) - max_len} chars] ...\n\n{text[-end_len:]}"
    
    async def analyze(
        self,
        droid_output: str,
        step_prompt: str,
        step_number: int,
        step_name: str,
        iteration: int,
        confirmations: int,
        failed_attempts: int,
        completion_criteria: list = None
    ) -> AnalysisResult:
        """Анализировать ответ Droid
        
        Args:
            droid_output: Ответ Droid
            step_prompt: ТЗ шага
            step_number: Номер шага (1-6)
            step_name: Название шага
            iteration: Номер итерации
            confirmations: Сколько раз подряд "нет изменений"
            failed_attempts: Сколько неудачных попыток подряд
            completion_criteria: Критерии выполнения шага
        
        Returns:
            AnalysisResult с решением
        """
        criteria_text = ""
        if completion_criteria:
            criteria_text = "\n".join(f"- {c}" for c in completion_criteria)
        
        prompt = f"""{self.SYSTEM_PROMPT}

КОНТЕКСТ ИТЕРАЦИИ:
- Шаг: {step_number}/6 ({step_name})
- Итерация: {iteration}
- Confirmations подряд (без изменений): {confirmations}/2
- Неудачных попыток подряд: {failed_attempts}

ТЗ ШАГА:
{step_prompt}

КРИТЕРИИ ВЫПОЛНЕНИЯ:
{criteria_text if criteria_text else "Не указаны"}

ОТВЕТ DROID:
{self._smart_truncate(droid_output, max_len=3000)}

Проанализируй и ответь JSON:
```json
{{
  "task_complete": true|false,
  "has_changes": true|false,
  "changes_substantial": true|false,
  "changes_description": "что именно изменил (если есть)",
  "issues": ["проблема 1", "проблема 2"],
  "action": "CONTINUE|ASK_DROID|ENFORCE_TASK|NEW_CHAT|ESCALATE",
  "message_to_droid": "если нужно что-то сказать Droid",
  "reason": "почему такое решение"
}}
```
"""
        
        try:
            result = await self.llm.generate_json(prompt, temperature=0.3)
            return AnalysisResult.from_dict(result)
        except Exception as e:
            # Fallback при ошибке парсинга
            return AnalysisResult(
                task_complete=False,
                has_changes=False,
                changes_substantial=False,
                changes_description="",
                issues=[f"Analysis error: {e}"],
                action=AnalysisAction.ASK_DROID,
                message_to_droid="Опиши что ты сделал и какие изменения внёс.",
                reason=f"Failed to parse analysis: {e}"
            )
    
    async def quick_check(self, droid_output: str) -> Dict[str, bool]:
        """Быстрая проверка ответа без полного анализа
        
        Returns:
            {"has_error": bool, "seems_complete": bool, "has_changes": bool}
        """
        output_lower = droid_output.lower()
        
        # Признаки ошибок
        error_patterns = ["error", "exception", "failed", "не удалось", "ошибка"]
        has_error = any(p in output_lower for p in error_patterns)
        
        # Признаки завершения
        complete_patterns = ["done", "complete", "готово", "finished", "всё работает", "no changes needed"]
        seems_complete = any(p in output_lower for p in complete_patterns)
        
        # Признаки изменений
        change_patterns = ["changed", "modified", "added", "created", "updated", "изменил", "добавил", "исправил"]
        has_changes = any(p in output_lower for p in change_patterns)
        
        return {
            "has_error": has_error,
            "seems_complete": seems_complete,
            "has_changes": has_changes
        }
