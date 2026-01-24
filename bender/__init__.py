"""Bender - AI Task Supervisor

Bender supervises AI CLI tools (copilot, droid, codex) to complete tasks.
It doesn't solve tasks itself, but ensures they are completed correctly.

Uses GLM (Cerebras zai-glm-4.7) as the only LLM - a thinking model.
Fallback: Qwen (qwen-3-235b-a22b-instruct-2507)
"""

# Core clients - GLM + Qwen fallback
from .glm_client import GLMClient, LLMUsage
from .llm_router import LLMRouter

# Workers
from .workers import (
    BaseWorker,
    WorkerStatus,
    WorkerResult,
    CopilotWorker,
    TokenUsage,
    DroidWorker,
    CodexWorker,
)
from .worker_manager import WorkerManager, WorkerType, ManagerConfig

# Log processing
from .log_filter import LogFilter, FilteredLog
from .log_watcher import LogWatcher, AnalysisResult, WatcherAnalysis
from .context_manager import ContextManager, ContextBudget

# Task management
from .task_clarifier import TaskClarifier, TaskComplexity, ClarifiedTask
from .task_manager import TaskManager, TaskState, TaskResult

# Legacy (for backwards compatibility)
from .supervisor import BenderSupervisor, SupervisorDecision
from .analyzer import ResponseAnalyzer, AnalysisAction
from .watchdog import Watchdog, HealthCheck, HealthStatus, WatchdogAction
from .enforcer import TaskEnforcer, EnforcementResult

__all__ = [
    # Core - GLM + Qwen fallback
    "GLMClient",
    "LLMUsage",
    "LLMRouter",
    # Workers
    "BaseWorker",
    "WorkerStatus",
    "WorkerResult",
    "CopilotWorker",
    "TokenUsage",
    "DroidWorker",
    "CodexWorker",
    "WorkerManager",
    "WorkerType",
    "ManagerConfig",
    # Log processing
    "LogFilter",
    "FilteredLog",
    "LogWatcher",
    "AnalysisResult",
    "WatcherAnalysis",
    "ContextManager",
    "ContextBudget",
    # Task management
    "TaskClarifier",
    "TaskComplexity",
    "ClarifiedTask",
    "TaskManager",
    "TaskState",
    "TaskResult",
    # Legacy
    "BenderSupervisor",
    "SupervisorDecision",
    "ResponseAnalyzer",
    "AnalysisAction",
    "Watchdog",
    "HealthCheck",
    "HealthStatus",
    "WatchdogAction",
    "TaskEnforcer",
    "EnforcementResult",
]
