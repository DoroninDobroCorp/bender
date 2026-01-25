"""
Codex Worker - worker для Codex CLI (сложные задачи)
"""

import logging
from typing import List, Optional

from .base import BaseWorker, WorkerConfig

logger = logging.getLogger(__name__)


class CodexWorker(BaseWorker):
    """Worker для Codex CLI
    
    Режим для сверхсложных задач: поиск сложных багов, детальное планирование.
    Использует dangerous mode 5.2 codex extra high.
    Интервал проверки логов x2 (задачи занимают больше времени).
    """
    
    WORKER_NAME = "codex"
    INTERVAL_MULTIPLIER = 2.0  # Проверяем в 2 раза реже
    
    def __init__(self, config: WorkerConfig):
        super().__init__(config)
    
    @property
    def cli_command(self) -> List[str]:
        """CLI команда для codex"""
        return [
            "codex",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
    
    def format_task(self, task: str, context: Optional[str] = None) -> str:
        """Форматировать задачу для codex
        
        Codex работает с более детальными инструкциями для сложных задач.
        """
        formatted = f"""СЛОЖНАЯ ЗАДАЧА (требует глубокого анализа):

{task}

Инструкции:
1. Тщательно проанализируй проблему
2. Изучи связанный код
3. Предложи и реализуй решение
4. Проверь, что решение работает
"""
        if context:
            formatted += f"\n\nКонтекст предыдущих попыток:\n{context}"
        return formatted
