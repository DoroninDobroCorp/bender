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
    
    CLARIFY_PROMPT = """Ты помощник по анализу технических заданий.

Рабочая директория: {project_path}

Задача от пользователя:
{task}

Твоя роль: НЕ ПЕРЕФОРМУЛИРОВАТЬ задачу, а только:
1. Определить сложность
2. Добавить чёткие acceptance criteria (критерии приёмки)

ВАЖНО:
- Если пользователь написал "не спрашивай", "делай", "без вопросов" и т.п. - НЕ задавай вопросов!
- НЕ переформулируй задачу - она уже сформулирована пользователем
- Только ДОБАВЬ acceptance criteria для проверки выполнения

Ответь в JSON:
{{
    "complexity": "SIMPLE|MEDIUM|COMPLEX",
    "is_clear": true,
    "acceptance_criteria": ["критерий 1", "критерий 2", ...],
    "questions": [],
    "needs_final_review": true/false
}}

Сложность:
- SIMPLE: одно действие (опечатка, простой файл)
- MEDIUM: несколько шагов (endpoint, тест)  
- COMPLEX: много изменений (новая фича, большой рефакторинг)
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
            ClarifiedTask с ОРИГИНАЛЬНОЙ задачей и acceptance criteria
        """
        logger.info(f"[Clarifier] Analyzing task: {task[:50]}...")
        
        # Проверяем есть ли указание не спрашивать
        task_lower = task.lower()
        skip_questions = any(phrase in task_lower for phrase in [
            "не спрашивай", "без вопросов", "делай", "просто сделай",
            "не задавай", "don't ask", "just do", "no questions"
        ])
        
        if skip_questions:
            logger.info("[Clarifier] User requested no questions, skipping clarification")
        
        # Первичный анализ
        prompt = self.CLARIFY_PROMPT.format(task=task, project_path=self.project_path)
        
        try:
            result = await self.llm.generate_json(prompt, temperature=0.3)
        except Exception as e:
            logger.warning(f"[Clarifier] Failed to analyze, using defaults: {e}")
            return ClarifiedTask(
                original_task=task,
                clarified_task=task,  # ОРИГИНАЛЬНАЯ задача
                complexity=TaskComplexity.MEDIUM,
                acceptance_criteria=["Задача выполнена"],
            )
        
        questions = result.get("questions", [])
        
        # Спрашиваем ТОЛЬКО если:
        # 1. Есть вопросы
        # 2. Есть callback
        # 3. Пользователь НЕ сказал "не спрашивай"
        if questions and self.on_ask_user and not skip_questions:
            logger.info(f"[Clarifier] Need clarification: {len(questions)} questions")
            
            answers = []
            for q in questions[:3]:
                answer = await self.on_ask_user(q)
                answers.append(answer)
            
            # Добавляем ответы к критериям, но НЕ переформулируем задачу
            # Просто обновляем criteria с учётом ответов
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
            clarified_task=task,  # ВСЕГДА оригинальная задача!
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
