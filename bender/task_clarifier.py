"""
Task Clarifier - уточнение ТЗ и определение сложности

GLM помогает сформулировать чёткие критерии выполнения задачи.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional, List, Callable, Awaitable
from enum import Enum

from .llm_router import LLMRouter

logger = logging.getLogger(__name__)


class TaskComplexity(str, Enum):
    """Сложность задачи"""
    SIMPLE = "simple"      # droid, без проверки
    MEDIUM = "medium"      # copilot (opus)
    COMPLEX = "complex"    # codex, с финальным review


@dataclass
class ClarifiedTask:
    """Результат уточнения задачи"""
    original_task: str
    clarified_task: str
    complexity: TaskComplexity
    acceptance_criteria: List[str] = field(default_factory=list)
    needs_final_review: bool = False
    
    def __str__(self) -> str:
        return f"[{self.complexity.value}] {self.clarified_task}"


class TaskClarifier:
    """Уточнение ТЗ через GLM"""
    
    CLARIFY_PROMPT = """Ты помощник по уточнению технических заданий.

Рабочая директория: {project_path}
(Все относительные пути и упоминания "эта папка", "текущая директория" относятся к ней)

Задача от пользователя:
{task}

Проанализируй задачу и определи:

1. **Сложность** (SIMPLE / MEDIUM / COMPLEX):
   - SIMPLE: одно действие, понятно что делать (исправить опечатку, добавить простой файл, запустить команду)
   - MEDIUM: несколько шагов, стандартная задача (добавить endpoint, написать тест, рефакторинг)
   - COMPLEX: много изменений, архитектура, баги, планирование (новая фича, найти сложный баг, рефакторинг большого модуля)

2. **Критерии выполнения** - чёткий список пунктов, по которым можно проверить что задача выполнена

3. **Нужно ли уточнение** - есть ли неясности которые надо спросить у пользователя?

ВАЖНО: Не задавай вопросы ради вопросов! Задавай только если реально нужно помочь пользователю чётко сформулировать задачу и найти чёткие критерии выполнения. Если задача понятна — questions должен быть пустым [].

Ответь в формате JSON:
{{
    "complexity": "SIMPLE|MEDIUM|COMPLEX",
    "is_clear": true/false,
    "clarified_task": "уточнённая формулировка задачи",
    "acceptance_criteria": ["критерий 1", "критерий 2", ...],
    "questions": ["вопрос 1", "вопрос 2", ...] или [] если всё ясно,
    "needs_final_review": true/false (true если много изменений ожидается)
}}
"""

    REFINE_PROMPT = """Пользователь уточнил задачу.

Исходная задача: {original_task}
Вопросы: {questions}
Ответы пользователя: {answers}

Теперь сформулируй окончательное ТЗ в формате JSON:
{{
    "complexity": "SIMPLE|MEDIUM|COMPLEX",
    "clarified_task": "финальная формулировка",
    "acceptance_criteria": ["критерий 1", "критерий 2", ...],
    "needs_final_review": true/false
}}
"""

    def __init__(
        self,
        llm: LLMRouter,
        on_ask_user: Optional[Callable[[str], Awaitable[str]]] = None,
        project_path: Optional[str] = None,
    ):
        self.llm = llm
        self.on_ask_user = on_ask_user
        self.project_path = project_path or "."
    
    async def clarify(self, task: str) -> ClarifiedTask:
        """Уточнить задачу
        
        Args:
            task: Исходная задача от пользователя
            
        Returns:
            ClarifiedTask с уточнённым ТЗ и критериями
        """
        logger.info(f"[Clarifier] Analyzing task: {task[:50]}...")
        
        # Первичный анализ
        prompt = self.CLARIFY_PROMPT.format(task=task, project_path=self.project_path)
        
        try:
            result = await self.llm.generate_json(prompt, temperature=0.3)
        except Exception as e:
            logger.warning(f"[Clarifier] Failed to analyze, using defaults: {e}")
            return ClarifiedTask(
                original_task=task,
                clarified_task=task,
                complexity=TaskComplexity.MEDIUM,
                acceptance_criteria=["Задача выполнена"],
            )
        
        is_clear = result.get("is_clear", True)
        questions = result.get("questions", [])
        
        # Если есть вопросы и есть callback для пользователя
        if not is_clear and questions and self.on_ask_user:
            logger.info(f"[Clarifier] Need clarification: {len(questions)} questions")
            
            # Спрашиваем пользователя
            answers = []
            for q in questions[:3]:  # макс 3 вопроса
                answer = await self.on_ask_user(q)
                answers.append(answer)
            
            # Уточняем ТЗ с ответами
            refine_prompt = self.REFINE_PROMPT.format(
                original_task=task,
                questions=questions[:3],
                answers=answers,
            )
            
            try:
                result = await self.llm.generate_json(refine_prompt, temperature=0.3)
            except Exception as e:
                logger.warning(f"[Clarifier] Failed to refine: {e}")
        
        # Парсим результат
        complexity_str = result.get("complexity", "MEDIUM").upper()
        try:
            complexity = TaskComplexity(complexity_str.lower())
        except ValueError:
            complexity = TaskComplexity.MEDIUM
        
        clarified = ClarifiedTask(
            original_task=task,
            clarified_task=result.get("clarified_task", task),
            complexity=complexity,
            acceptance_criteria=result.get("acceptance_criteria", ["Задача выполнена"]),
            needs_final_review=result.get("needs_final_review", False),
        )
        
        logger.info(f"[Clarifier] Result: {clarified.complexity.value}, {len(clarified.acceptance_criteria)} criteria")
        return clarified
    
    async def quick_assess(self, task: str) -> TaskComplexity:
        """Быстрая оценка сложности без уточнений
        
        Для случаев когда нужно только определить worker'а.
        """
        # Простые эвристики
        task_lower = task.lower()
        
        # SIMPLE
        simple_keywords = [
            "echo", "ls", "cat", "pwd", "опечатк", "typo", "fix typo",
            "readme", "comment", "print", "log", "покажи", "выведи",
        ]
        if any(kw in task_lower for kw in simple_keywords):
            return TaskComplexity.SIMPLE
        
        # COMPLEX
        complex_keywords = [
            "баг", "bug", "утечк", "leak", "архитектур", "рефактор",
            "мигр", "планир", "design", "разработа", "implement",
            "oauth", "auth", "database", "api", "интеграц",
        ]
        if any(kw in task_lower for kw in complex_keywords):
            return TaskComplexity.COMPLEX
        
        # По длине
        if len(task) < 30:
            return TaskComplexity.SIMPLE
        if len(task) > 200:
            return TaskComplexity.COMPLEX
        
        return TaskComplexity.MEDIUM
