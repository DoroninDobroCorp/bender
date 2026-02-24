"""Review Loop v2 — простой цикл без парсинга findings.

Экспорт:
    ReviewLoopManager — основной класс цикла
    ReviewLoopResult  — результат выполнения
    Finding           — stub для backward compatibility
"""

from .loop import ReviewLoopManager, ReviewLoopResult, Finding

__all__ = [
    "ReviewLoopManager",
    "ReviewLoopResult",
    "Finding",
]
