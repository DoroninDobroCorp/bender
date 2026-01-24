"""
Droid Worker - worker для Droid CLI (простые задачи)
"""

import logging
from typing import List, Optional

from .base import BaseWorker, WorkerConfig

logger = logging.getLogger(__name__)


class DroidWorker(BaseWorker):
    """Worker для Droid CLI
    
    Режим для простых задач. Использует sonnet от kiro или fallback на gemini 3 pro.
    """
    
    WORKER_NAME = "droid"
    INTERVAL_MULTIPLIER = 1.0
    
    # Модели в порядке приоритета
    PRIMARY_MODEL = "sonnet"  # от kiro
    FALLBACK_MODEL = "gemini-3-pro"  # от antigravity
    
    def __init__(self, config: WorkerConfig, model: Optional[str] = None):
        super().__init__(config)
        self.model = model or self.PRIMARY_MODEL
    
    @property
    def cli_command(self) -> List[str]:
        """CLI команда для droid"""
        return [
            "droid",
            "--model", self.model,
        ]
    
    def format_task(self, task: str, context: Optional[str] = None) -> str:
        """Форматировать задачу для droid"""
        if context:
            return f"{task}\n\nПредыдущий контекст:\n{context}"
        return task
