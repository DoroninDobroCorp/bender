"""
Droid Worker - worker для Droid CLI (простые задачи)
"""

import logging
from typing import List, Optional

from .base import BaseWorker, WorkerConfig

logger = logging.getLogger(__name__)


class DroidWorker(BaseWorker):
    """Worker для Droid CLI
    
    Режим для простых задач. Droid использует модель по умолчанию (настраивается в droid config).
    """
    
    WORKER_NAME = "droid"
    INTERVAL_MULTIPLIER = 1.0
    
    def __init__(self, config: WorkerConfig):
        super().__init__(config)
    
    @property
    def cli_command(self) -> List[str]:
        """CLI команда для droid"""
        return ["droid"]
    
    def format_task(self, task: str, context: Optional[str] = None) -> str:
        """Форматировать задачу для droid"""
        if context:
            return f"{task}\n\nПредыдущий контекст:\n{context}"
        return task
