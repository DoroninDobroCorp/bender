"""
Bender Supervisor - главный координатор

Объединяет:
- Watchdog (мониторинг здоровья)
- Analyzer (анализ ответов)
- Enforcer (настаивание на ТЗ)
- LLM Router (Gemini + GLM)
"""

import asyncio
import logging
from typing import Optional, Callable, Awaitable
from dataclasses import dataclass

from .llm_router import LLMRouter
from .analyzer import ResponseAnalyzer, AnalysisResult, AnalysisAction
from .watchdog import Watchdog, HealthCheck, WatchdogAction, HealthStatus
from .enforcer import TaskEnforcer, EnforcementResult


logger = logging.getLogger(__name__)


@dataclass
class SupervisorDecision:
    """Решение супервизора"""
    action: str                    # CONTINUE, NEW_CHAT, ESCALATE, SEND_MESSAGE
    message: Optional[str] = None  # Сообщение для Droid
    reason: str = ""               # Причина решения
    analysis: Optional[AnalysisResult] = None
    health: Optional[HealthCheck] = None


class BenderSupervisor:
    """Главный супервизор Bender
    
    Координирует все компоненты и принимает решения.
    """
    
    def __init__(
        self,
        glm_api_key: str,
        gemini_api_key: Optional[str] = None,
        escalate_after: int = 5,
        watchdog_interval: int = 300,
        watchdog_timeout: int = 3600,
        display_mode: str = "visible"
    ):
        # LLM Router
        self.llm = LLMRouter(
            glm_api_key=glm_api_key,
            gemini_api_key=gemini_api_key
        )
        
        # Компоненты
        self.analyzer = ResponseAnalyzer(self.llm)
        self.watchdog = Watchdog(
            check_interval=watchdog_interval,
            stuck_threshold=watchdog_timeout
        )
        self.enforcer = TaskEnforcer(
            max_attempts=escalate_after,
            llm_router=self.llm
        )
        
        # Настройки
        self.display_mode = display_mode
        self.escalate_after = escalate_after
        
        # Состояние
        self._confirmations = 0
        self._failed_attempts = 0
        self._watchdog_task: Optional[asyncio.Task] = None
        
        # Callbacks
        self._on_escalate: Optional[Callable[[str], Awaitable[None]]] = None
        self._on_decision: Optional[Callable[[SupervisorDecision], None]] = None
    
    def set_callbacks(
        self,
        on_escalate: Callable[[str], Awaitable[None]] = None,
        on_decision: Callable[[SupervisorDecision], None] = None
    ):
        """Установить callbacks"""
        self._on_escalate = on_escalate
        self._on_decision = on_decision
    
    async def analyze_response(
        self,
        droid_output: str,
        step_prompt: str,
        step_number: int,
        step_name: str,
        iteration: int,
        completion_criteria: list = None
    ) -> SupervisorDecision:
        """Анализировать ответ Droid и принять решение
        
        Returns:
            SupervisorDecision с действием
        """
        # Анализ через Gemini
        analysis = await self.analyzer.analyze(
            droid_output=droid_output,
            step_prompt=step_prompt,
            step_number=step_number,
            step_name=step_name,
            iteration=iteration,
            confirmations=self._confirmations,
            failed_attempts=self._failed_attempts,
            completion_criteria=completion_criteria
        )
        
        self._log_analysis(analysis)
        
        # Принять решение на основе анализа
        decision = await self._make_decision(analysis, step_prompt, droid_output)
        
        if self._on_decision:
            self._on_decision(decision)
        
        return decision
    
    async def _make_decision(
        self,
        analysis: AnalysisResult,
        step_prompt: str,
        droid_output: str
    ) -> SupervisorDecision:
        """Принять решение на основе анализа"""
        
        # ESCALATE
        if analysis.action == AnalysisAction.ESCALATE:
            if self._on_escalate:
                await self._on_escalate(analysis.reason)
            return SupervisorDecision(
                action="ESCALATE",
                reason=analysis.reason,
                analysis=analysis
            )
        
        # ENFORCE_TASK
        if analysis.action == AnalysisAction.ENFORCE_TASK:
            self._failed_attempts += 1
            
            enforcement = await self.enforcer.enforce_with_llm(
                missing_items=analysis.issues,
                step_prompt=step_prompt,
                droid_response=droid_output,
                issues=analysis.issues
            )
            
            if enforcement.should_escalate:
                if self._on_escalate:
                    await self._on_escalate("Max enforcement attempts reached")
                return SupervisorDecision(
                    action="ESCALATE",
                    reason="Max enforcement attempts reached",
                    analysis=analysis
                )
            
            return SupervisorDecision(
                action="SEND_MESSAGE",
                message=enforcement.message,
                reason=f"Task not complete, attempt {enforcement.attempt}",
                analysis=analysis
            )
        
        # ASK_DROID
        if analysis.action == AnalysisAction.ASK_DROID:
            return SupervisorDecision(
                action="SEND_MESSAGE",
                message=analysis.message_to_droid or "Опиши что ты сделал и какие изменения внёс.",
                reason="Need clarification",
                analysis=analysis
            )
        
        # NEW_CHAT (существенные изменения)
        if analysis.action == AnalysisAction.NEW_CHAT:
            self._confirmations = 0
            self._failed_attempts = 0
            self.enforcer.reset()
            return SupervisorDecision(
                action="NEW_CHAT",
                reason=f"Substantial changes: {analysis.changes_description}",
                analysis=analysis
            )
        
        # CONTINUE
        if analysis.action == AnalysisAction.CONTINUE:
            if not analysis.has_changes:
                self._confirmations += 1
            elif not analysis.changes_substantial:
                # Minor changes (typos, formatting) also count toward confirmation
                self._confirmations += 1
            # Substantial changes reset confirmations (handled by NEW_CHAT action)
            
            self._failed_attempts = 0
            self.enforcer.reset()
            
            return SupervisorDecision(
                action="CONTINUE",
                reason=f"Confirmations: {self._confirmations}/2",
                analysis=analysis
            )
        
        # Default
        return SupervisorDecision(
            action="CONTINUE",
            reason="Default action",
            analysis=analysis
        )
    
    async def handle_health_issue(self, health: HealthCheck) -> SupervisorDecision:
        """Обработать проблему со здоровьем Droid"""
        self._log_health(health)
        
        if health.action == WatchdogAction.ESCALATE:
            if self._on_escalate:
                await self._on_escalate(health.reason)
            return SupervisorDecision(
                action="ESCALATE",
                reason=health.reason,
                health=health
            )
        
        if health.action == WatchdogAction.RESTART:
            return SupervisorDecision(
                action="RESTART",
                reason=health.reason,
                health=health
            )
        
        if health.action == WatchdogAction.NEW_CHAT:
            return SupervisorDecision(
                action="NEW_CHAT",
                reason=health.reason,
                health=health
            )
        
        if health.action == WatchdogAction.PING:
            return SupervisorDecision(
                action="PING",
                reason=health.reason,
                health=health
            )
        
        return SupervisorDecision(
            action="WAIT",
            reason="Waiting",
            health=health
        )
    
    def start_watchdog(
        self,
        get_output: Callable[[], str],
        is_alive: Callable[[], bool],
        on_issue: Callable[[SupervisorDecision], Awaitable[None]]
    ):
        """Запустить watchdog в фоне"""
        async def handle_issue(health: HealthCheck):
            decision = await self.handle_health_issue(health)
            await on_issue(decision)
        
        self._watchdog_task = asyncio.create_task(
            self.watchdog.start_monitoring(get_output, is_alive, handle_issue)
        )
    
    def stop_watchdog(self):
        """Остановить watchdog с graceful shutdown"""
        self.watchdog.stop_monitoring()
        if self._watchdog_task:
            self._watchdog_task.cancel()
            # Note: Task will be awaited in _cleanup() or garbage collected
            self._watchdog_task = None
    
    @property
    def confirmations(self) -> int:
        """Текущее количество confirmations"""
        return self._confirmations
    
    def reset_confirmations(self):
        """Сбросить confirmations"""
        self._confirmations = 0
    
    def reset_state(self):
        """Полный сброс состояния"""
        self._confirmations = 0
        self._failed_attempts = 0
        self.enforcer.reset()
        self.watchdog.reset()
    
    def _log_analysis(self, analysis: AnalysisResult):
        """Логировать анализ"""
        if self.display_mode == "visible":
            logger.info(f"Analysis: action={analysis.action}, task_complete={analysis.task_complete}, "
                       f"has_changes={analysis.has_changes}, substantial={analysis.changes_substantial}")
            if analysis.reason:
                logger.info(f"Reason: {analysis.reason}")
    
    def _log_health(self, health: HealthCheck):
        """Логировать health check"""
        if self.display_mode == "visible":
            logger.info(f"Health: status={health.status}, action={health.action}, reason={health.reason}")
    
    def get_stats(self) -> dict:
        """Получить статистику"""
        return {
            "confirmations": self._confirmations,
            "failed_attempts": self._failed_attempts,
            "enforcer_attempts": self.enforcer.attempts,
            "llm_stats": self.llm.get_stats()
        }
