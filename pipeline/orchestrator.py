"""
Pipeline Orchestrator - координатор 6-шагового pipeline

Управляет:
- Загрузкой и выполнением шагов
- Логикой 2x confirmation для перехода
- Git commit после существенных изменений
- Новый чат после каждой итерации с изменениями
"""

import asyncio
import logging
from typing import Optional, Callable, Awaitable, Dict, Any
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .step import Step, StepConfig, load_steps
from .git_manager import GitManager, GitResult
from core.droid_controller import DroidController
from bender.supervisor import BenderSupervisor, SupervisorDecision
from bender.analyzer import AnalysisAction


logger = logging.getLogger(__name__)


class PipelineStatus(str, Enum):
    """Статус pipeline"""
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ESCALATED = "ESCALATED"


@dataclass
class StepState:
    """Состояние шага"""
    step_id: int
    iteration: int = 0
    confirmations: int = 0
    completed: bool = False


@dataclass
class PipelineState:
    """Состояние всего pipeline"""
    current_step: int = 1
    status: PipelineStatus = PipelineStatus.IDLE
    steps: Dict[int, StepState] = field(default_factory=dict)
    total_iterations: int = 0
    total_commits: int = 0
    
    def get_step_state(self, step_id: int) -> StepState:
        if step_id not in self.steps:
            self.steps[step_id] = StepState(step_id=step_id)
        return self.steps[step_id]


@dataclass
class PipelineConfig:
    """Конфигурация pipeline"""
    target_url: str = ""
    parse_target: str = ""
    # Дополнительные переменные для промптов
    extra_vars: Dict[str, str] = field(default_factory=dict)


class PipelineOrchestrator:
    """Оркестратор 6-шагового pipeline"""
    
    CONFIRMATIONS_REQUIRED = 2
    
    def __init__(
        self,
        project_path: str,
        gemini_api_key: str,
        glm_api_key: Optional[str] = None,
        steps_yaml: Optional[str] = None,
        auto_git_push: bool = True,
        display_mode: str = "visible",
        escalate_after: int = 5
    ):
        self.project_path = Path(project_path)
        
        # Загрузить шаги
        self.step_config = load_steps(steps_yaml)
        
        # Компоненты
        self.droid: Optional[DroidController] = None
        self.bender = BenderSupervisor(
            gemini_api_key=gemini_api_key,
            glm_api_key=glm_api_key,
            escalate_after=escalate_after,
            display_mode=display_mode
        )
        self.git = GitManager(
            project_path=project_path,
            auto_push=auto_git_push
        )
        
        # Состояние
        self.state = PipelineState()
        self.config = PipelineConfig()
        
        # Callbacks
        self._on_step_complete: Optional[Callable[[int, StepState], Awaitable[None]]] = None
        self._on_pipeline_complete: Optional[Callable[[PipelineState], Awaitable[None]]] = None
        self._on_escalate: Optional[Callable[[str], Awaitable[None]]] = None
        self._on_progress: Optional[Callable[[str], None]] = None
    
    def set_callbacks(
        self,
        on_step_complete: Callable[[int, StepState], Awaitable[None]] = None,
        on_pipeline_complete: Callable[[PipelineState], Awaitable[None]] = None,
        on_escalate: Callable[[str], Awaitable[None]] = None,
        on_progress: Callable[[str], None] = None
    ):
        """Установить callbacks"""
        self._on_step_complete = on_step_complete
        self._on_pipeline_complete = on_pipeline_complete
        self._on_escalate = on_escalate
        self._on_progress = on_progress
        
        # Передать escalate callback в Bender
        self.bender.set_callbacks(on_escalate=on_escalate)
    
    def configure(
        self,
        target_url: str = "",
        parse_target: str = "",
        **extra_vars
    ):
        """Настроить параметры для промптов"""
        self.config.target_url = target_url
        self.config.parse_target = parse_target
        self.config.extra_vars = extra_vars
    
    async def run(self) -> PipelineState:
        """Запустить pipeline с первого шага"""
        return await self.run_from_step(1)
    
    async def run_from_step(self, start_step: int) -> PipelineState:
        """Запустить pipeline с указанного шага"""
        self.state.status = PipelineStatus.RUNNING
        self.state.current_step = start_step
        
        self._log_progress(f"Starting pipeline from step {start_step}")
        
        try:
            # Запустить Droid
            await self._start_droid()
            
            # Выполнить шаги
            for step_id in range(start_step, self.step_config.total_steps + 1):
                self.state.current_step = step_id
                step = self.step_config.get_step(step_id)
                
                if step is None:
                    logger.error(f"Step {step_id} not found")
                    continue
                
                self._log_progress(f"Step {step_id}/{self.step_config.total_steps}: {step.name}")
                
                # Выполнить шаг
                success = await self._run_step(step)
                
                if not success:
                    if self.state.status == PipelineStatus.ESCALATED:
                        break
                    # Попробовать продолжить со следующего шага
                    logger.warning(f"Step {step_id} failed, continuing...")
                
                # Callback
                if self._on_step_complete:
                    await self._on_step_complete(step_id, self.state.get_step_state(step_id))
            
            # Завершение
            if self.state.status == PipelineStatus.RUNNING:
                self.state.status = PipelineStatus.COMPLETED
                self._log_progress("Pipeline completed!")
            
            if self._on_pipeline_complete:
                await self._on_pipeline_complete(self.state)
            
        except Exception as e:
            logger.error(f"Pipeline error: {e}")
            self.state.status = PipelineStatus.FAILED
            raise
        finally:
            await self._stop_droid()
        
        return self.state
    
    async def _run_step(self, step: Step) -> bool:
        """Выполнить один шаг до 2x confirmation"""
        step_state = self.state.get_step_state(step.id)
        
        # Получить промпт с переменными
        prompt_vars = {
            "target_url": self.config.target_url,
            "parse_target": self.config.parse_target,
            **self.config.extra_vars
        }
        prompt = step.get_prompt(**prompt_vars)
        
        while not step_state.completed:
            step_state.iteration += 1
            self.state.total_iterations += 1
            
            self._log_progress(f"  Iteration {step_state.iteration}, confirmations: {step_state.confirmations}/{self.CONFIRMATIONS_REQUIRED}")
            
            # Отправить промпт Droid
            response = await self.droid.send(prompt, timeout=300)
            
            # Проверить approval request
            if self.droid.has_approval_request():
                await self.droid.approve("Yes")
                response = await self.droid.send("Continue", timeout=300)
            
            # Анализ через Bender
            decision = await self.bender.analyze_response(
                droid_output=response,
                step_prompt=prompt,
                step_number=step.id,
                step_name=step.name,
                iteration=step_state.iteration,
                completion_criteria=step.completion_criteria
            )
            
            # Обработать решение
            result = await self._handle_decision(decision, step, step_state)
            
            if result == "NEXT_STEP":
                step_state.completed = True
                return True
            elif result == "ESCALATE":
                self.state.status = PipelineStatus.ESCALATED
                return False
            # else: CONTINUE - продолжаем цикл
        
        return True
    
    async def _handle_decision(
        self,
        decision: SupervisorDecision,
        step: Step,
        step_state: StepState
    ) -> str:
        """Обработать решение Bender
        
        Returns:
            "CONTINUE" | "NEXT_STEP" | "ESCALATE"
        """
        logger.info(f"Decision: {decision.action} - {decision.reason}")
        
        if decision.action == "ESCALATE":
            if self._on_escalate:
                await self._on_escalate(decision.reason)
            return "ESCALATE"
        
        if decision.action == "SEND_MESSAGE":
            # Отправить сообщение Droid
            await self.droid.send(decision.message, timeout=300)
            return "CONTINUE"
        
        if decision.action == "NEW_CHAT":
            # Существенные изменения - git commit и новый чат
            step_state.confirmations = 0
            self.bender.reset_confirmations()
            
            # Git commit
            summary = ""
            if decision.analysis and decision.analysis.changes_description:
                summary = decision.analysis.changes_description
            
            git_result = self.git.commit_and_push(
                step_number=step.id,
                iteration=step_state.iteration,
                summary=summary
            )
            
            if git_result.needs_human:
                logger.warning(f"Git issue: {git_result.error}")
                if self._on_escalate:
                    await self._on_escalate(f"Git error: {git_result.error}")
            else:
                self.state.total_commits += 1
            
            # Новый чат
            await self.droid.new_chat()
            return "CONTINUE"
        
        if decision.action == "CONTINUE":
            # Проверить confirmations
            step_state.confirmations = self.bender.confirmations
            
            if step_state.confirmations >= self.CONFIRMATIONS_REQUIRED:
                self._log_progress(f"  Step {step.id} complete (2x confirmation)")
                return "NEXT_STEP"
            
            return "CONTINUE"
        
        # Default
        return "CONTINUE"
    
    async def _start_droid(self):
        """Запустить Droid"""
        self.droid = DroidController(
            project_path=str(self.project_path),
            log_dir=str(self.project_path / "logs")
        )
        await self.droid.start()
        
        # Запустить watchdog
        self.bender.start_watchdog(
            get_output=self.droid.get_current_output,
            is_alive=self.droid.is_running,
            on_issue=self._handle_watchdog_issue
        )
    
    async def _stop_droid(self):
        """Остановить Droid"""
        self.bender.stop_watchdog()
        if self.droid and self.droid.is_running():
            await self.droid.stop()
    
    async def _handle_watchdog_issue(self, decision: SupervisorDecision):
        """Обработать проблему от watchdog"""
        logger.warning(f"Watchdog issue: {decision.action} - {decision.reason}")
        
        if decision.action == "ESCALATE":
            self.state.status = PipelineStatus.ESCALATED
            if self._on_escalate:
                await self._on_escalate(decision.reason)
        
        elif decision.action == "RESTART":
            await self._stop_droid()
            await asyncio.sleep(2)
            await self._start_droid()
        
        elif decision.action == "NEW_CHAT":
            await self.droid.new_chat()
        
        elif decision.action == "PING":
            # Отправить Enter
            await self.droid.send("", timeout=10)
    
    def _log_progress(self, message: str):
        """Логировать прогресс"""
        logger.info(message)
        if self._on_progress:
            self._on_progress(message)
    
    def get_state(self) -> PipelineState:
        """Получить текущее состояние"""
        return self.state
    
    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику"""
        return {
            "status": self.state.status.value,
            "current_step": self.state.current_step,
            "total_steps": self.step_config.total_steps,
            "total_iterations": self.state.total_iterations,
            "total_commits": self.state.total_commits,
            "bender_stats": self.bender.get_stats()
        }
