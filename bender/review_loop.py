"""
Review Loop Manager - итеративный цикл copilot → codex → copilot

Логика:
1. GLM анализирует задачу, формирует acceptance criteria
2. Copilot выполняет задачу
3. Codex/Copilot проверяет код (BMAD роли, визуально, тесты)
4. GLM анализирует findings и решает: исправлять или завершить
5. Если нужно исправить → новый Copilot
6. До MAX_ITERATIONS или пока GLM не скажет "готово"
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional, List, Callable, Awaitable
from enum import Enum

from .worker_manager import WorkerManager, WorkerType, ManagerConfig
from .llm_router import LLMRouter
from .task_clarifier import TaskClarifier, ClarifiedTask

logger = logging.getLogger(__name__)


class LoopDecision(str, Enum):
    """Решение GLM по findings"""
    FIX = "fix"      # Нужно исправить
    SKIP = "skip"    # Можно пропустить
    DONE = "done"    # Всё готово


@dataclass
class Finding:
    """Одна проблема от codex"""
    severity: str  # CRITICAL, HIGH, MEDIUM, LOW
    description: str
    location: Optional[str] = None


@dataclass
class LoopIteration:
    """Результат одной итерации"""
    iteration: int
    worker: str  # copilot или codex
    findings: List[Finding] = field(default_factory=list)
    decision: Optional[LoopDecision] = None
    fix_instructions: Optional[str] = None


@dataclass
class ReviewLoopResult:
    """Финальный результат review loop"""
    success: bool
    iterations: int
    total_findings: int
    fixed_findings: int
    remaining_findings: List[Finding] = field(default_factory=list)
    history: List[LoopIteration] = field(default_factory=list)


ANALYZE_FINDINGS_PROMPT = """Ты анализируешь результаты code review от Codex.

ЗАДАЧА которую выполняли: {task}

FINDINGS от Codex:
{findings}

Итерация: {iteration} из {max_iterations}

Проанализируй findings и реши что делать:
- CRITICAL/HIGH проблемы обычно НАДО исправить
- MEDIUM проблемы желательно исправить если это не займёт много времени
- LOW проблемы на твоё усмотрение — можно исправить если просто, можно пропустить

Если findings пустые или только незначительные замечания — можно завершить.
Если осталось мало итераций — фокусируйся только на критичном.

Ответь JSON:
{{
    "decision": "fix" | "skip" | "done",
    "reason": "почему такое решение",
    "critical_issues": ["список критичных проблем если есть"],
    "fix_instructions": "конкретные инструкции что исправить (если decision=fix)"
}}

ТОЛЬКО JSON, без комментариев."""


REVIEW_TASK = """Проведи ДОТОШНУЮ проверку кода:

Контекст задачи: {context}

Критерии приёмки:
{criteria}

Проверь:
1. Код на ошибки, баги, уязвимости
2. Соответствие КАЖДОМУ критерию приёмки выше
3. Запусти проект если нужно, сделай скриншоты
4. Проверь визуально что всё работает
5. Проанализируй с точки зрения КАЖДОЙ роли BMAD:
   - Developer: качество кода, паттерны
   - Architect: архитектура, API контракты
   - Test Architect: покрытие тестами
   - UX Designer: юзабилити, визуал
   - Business Analyst: соответствие требованиям
   - Scrum Master: Definition of Done

Будь дотошным! Лучше найти больше проблем чем пропустить.

Выведи findings в формате:
- CRITICAL/HIGH/MEDIUM/LOW: описание проблемы. файл:строка"""


class ReviewLoopManager:
    """Менеджер итеративного цикла review"""
    
    MAX_ITERATIONS = 10
    
    def __init__(
        self,
        llm: LLMRouter,
        manager_config: ManagerConfig,
        on_status: Optional[Callable[[str], Awaitable[None]]] = None,
        use_copilot_reviewer: bool = False,
    ):
        self.llm = llm
        self.config = manager_config
        self.on_status = on_status
        self.use_copilot_reviewer = use_copilot_reviewer
        self.history: List[LoopIteration] = []
        self._stop_requested = False
    
    @property
    def reviewer_type(self) -> WorkerType:
        """Какой воркер используем для review"""
        return WorkerType.OPUS if self.use_copilot_reviewer else WorkerType.CODEX
    
    @property
    def reviewer_name(self) -> str:
        return "copilot" if self.use_copilot_reviewer else "codex"
    
    def request_stop(self) -> None:
        """Запросить остановку"""
        self._stop_requested = True
    
    async def _report(self, message: str) -> None:
        """Отправить статус"""
        logger.info(f"[ReviewLoop] {message}")
        if self.on_status:
            await self.on_status(f"[Loop] {message}")
    
    async def _clarify_task(self, task: str) -> Optional[ClarifiedTask]:
        """Уточнить задачу через GLM"""
        try:
            clarifier = TaskClarifier(
                llm=self.llm,
                project_path=self.config.project_path,
            )
            return await clarifier.clarify(task)
        except Exception as e:
            logger.warning(f"[ReviewLoop] Failed to clarify task: {e}")
            return None
    
    def _format_task_with_criteria(self, clarified: ClarifiedTask) -> str:
        """Форматировать задачу с критериями для Copilot"""
        criteria_text = "\n".join([f"  {i+1}. {c}" for i, c in enumerate(clarified.acceptance_criteria)])
        return f"""{clarified.clarified_task}

📝 Acceptance Criteria:
{criteria_text}

Выполни ВСЕ пункты. После завершения проверь что каждый критерий выполнен."""
    
    def _format_criteria(self, clarified: Optional[ClarifiedTask]) -> str:
        """Форматировать критерии для review"""
        if not clarified or not clarified.acceptance_criteria:
            return "Нет явных критериев"
        return "\n".join([f"- {c}" for c in clarified.acceptance_criteria])
    
    async def run_loop(
        self,
        task: str,
        max_iterations: Optional[int] = None,
    ) -> ReviewLoopResult:
        """Запустить итеративный цикл review
        
        Args:
            task: Исходная задача
            max_iterations: Максимум итераций (default: MAX_ITERATIONS)
        
        Returns:
            ReviewLoopResult с результатами
        """
        max_iter = max_iterations or self.MAX_ITERATIONS
        total_findings = 0
        fixed_findings = 0
        
        await self._report(f"Starting review loop (max {max_iter} iterations)")
        
        # 0. Анализ и уточнение задачи через GLM
        await self._report("Analyzing task with GLM...")
        clarified = await self._clarify_task(task)
        
        if clarified:
            await self._report(f"Complexity: {clarified.complexity.value}")
            await self._report(f"Acceptance criteria: {len(clarified.acceptance_criteria)} items")
            current_task = self._format_task_with_criteria(clarified)
        else:
            await self._report("Using original task (clarification failed)")
            current_task = task
        
        for i in range(max_iter):
            if self._stop_requested:
                await self._report("Stopped by user")
                break
            
            iteration_num = i + 1
            await self._report(f"=== Iteration {iteration_num}/{max_iter} ===")
            
            # 1. Запустить Copilot
            await self._report(f"Running Copilot with task...")
            copilot_output = await self._run_worker(
                WorkerType.OPUS, 
                current_task,
                f"copilot-iter-{iteration_num}"
            )
            
            if self._stop_requested:
                break
            
            # 2. Запустить review (codex или copilot)
            await self._report(f"Running {self.reviewer_name} review...")
            review_task = REVIEW_TASK.format(
                context=task,
                criteria=self._format_criteria(clarified) if clarified else "Нет критериев"
            )
            review_output = await self._run_worker(
                self.reviewer_type,
                review_task,
                f"{self.reviewer_name}-iter-{iteration_num}"
            )
            
            if self._stop_requested:
                break
            
            # 3. Парсить findings
            findings = self._parse_findings(review_output)
            total_findings += len(findings)
            
            iteration = LoopIteration(
                iteration=iteration_num,
                worker=self.reviewer_name,
                findings=findings,
            )
            
            await self._report(f"Found {len(findings)} issues")
            
            # 4. Спросить GLM что делать
            decision, fix_instructions = await self._analyze_findings(
                task, findings, iteration_num, max_iter
            )
            
            iteration.decision = decision
            iteration.fix_instructions = fix_instructions
            self.history.append(iteration)
            
            await self._report(f"GLM decision: {decision.value}")
            
            # 5. Принять решение
            if decision == LoopDecision.DONE:
                await self._report("✅ Review complete - no more fixes needed")
                return ReviewLoopResult(
                    success=True,
                    iterations=iteration_num,
                    total_findings=total_findings,
                    fixed_findings=fixed_findings,
                    remaining_findings=findings,
                    history=self.history,
                )
            
            if decision == LoopDecision.SKIP:
                await self._report("⏭️ Skipping remaining issues")
                return ReviewLoopResult(
                    success=True,
                    iterations=iteration_num,
                    total_findings=total_findings,
                    fixed_findings=fixed_findings,
                    remaining_findings=findings,
                    history=self.history,
                )
            
            # decision == FIX
            fixed_findings += len([f for f in findings if f.severity in ("CRITICAL", "HIGH")])
            current_task = self._prepare_fix_task(task, findings, fix_instructions)
            await self._report(f"Preparing fixes for next iteration...")
        
        # Достигли максимума итераций
        await self._report(f"⚠️ Reached max iterations ({max_iter})")
        return ReviewLoopResult(
            success=False,
            iterations=max_iter,
            total_findings=total_findings,
            fixed_findings=fixed_findings,
            remaining_findings=self.history[-1].findings if self.history else [],
            history=self.history,
        )
    
    async def _run_worker(
        self, 
        worker_type: WorkerType, 
        task: str,
        session_suffix: str
    ) -> str:
        """Запустить worker и дождаться результата"""
        worker_manager = WorkerManager(
            config=self.config,
            on_output=None,
        )
        
        try:
            await worker_manager.start_task(task, worker_type)
            
            # Для copilot - wait_for_completion
            if worker_type == WorkerType.OPUS:
                success, output = await worker_manager.wait_for_completion(timeout=1800)
            else:
                # Для codex - мониторим лог
                output = ""
                for _ in range(60):  # 30 минут макс
                    await asyncio.sleep(30)
                    if not worker_manager.is_running:
                        break
                    new_output = await worker_manager.get_output()
                    if new_output:
                        output = new_output
                
                success = True
            
            return output
        finally:
            await worker_manager.stop()
    
    def _parse_findings(self, codex_output: str) -> List[Finding]:
        """Парсить findings из вывода codex"""
        findings = []
        
        # Ищем строки типа "- MEDIUM: description. file:line"
        import re
        pattern = r'-\s*(CRITICAL|HIGH|MEDIUM|LOW):\s*(.+?)(?:\.\s*(\S+:\d+))?$'
        
        for line in codex_output.split('\n'):
            match = re.match(pattern, line.strip())
            if match:
                severity, description, location = match.groups()
                findings.append(Finding(
                    severity=severity,
                    description=description.strip(),
                    location=location,
                ))
        
        # Если не нашли по паттерну, ищем просто упоминания severity
        if not findings:
            for line in codex_output.split('\n'):
                line = line.strip()
                for sev in ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW'):
                    if sev in line and ':' in line:
                        parts = line.split(':', 1)
                        if len(parts) == 2:
                            findings.append(Finding(
                                severity=sev,
                                description=parts[1].strip()[:200],
                                location=None,
                            ))
                        break
        
        return findings
    
    async def _analyze_findings(
        self,
        task: str,
        findings: List[Finding],
        iteration: int,
        max_iterations: int,
    ) -> tuple[LoopDecision, Optional[str]]:
        """Спросить GLM что делать с findings"""
        
        if not findings:
            return LoopDecision.DONE, None
        
        findings_text = "\n".join([
            f"- {f.severity}: {f.description}" + (f" ({f.location})" if f.location else "")
            for f in findings
        ])
        
        prompt = ANALYZE_FINDINGS_PROMPT.format(
            task=task,
            findings=findings_text,
            iteration=iteration,
            max_iterations=max_iterations,
        )
        
        try:
            result = await self.llm.generate_json(prompt, temperature=0.3)
            
            decision_str = result.get("decision", "done").lower()
            decision = LoopDecision(decision_str) if decision_str in ("fix", "skip", "done") else LoopDecision.DONE
            
            fix_instructions = result.get("fix_instructions")
            reason = result.get("reason", "")
            
            logger.info(f"[ReviewLoop] GLM reason: {reason}")
            
            return decision, fix_instructions
            
        except Exception as e:
            logger.warning(f"[ReviewLoop] Failed to analyze findings: {e}")
            # По умолчанию — если есть CRITICAL/HIGH, фиксим
            has_critical = any(f.severity in ("CRITICAL", "HIGH") for f in findings)
            if has_critical:
                return LoopDecision.FIX, "Fix critical and high severity issues"
            return LoopDecision.DONE, None
    
    def _prepare_fix_task(
        self,
        original_task: str,
        findings: List[Finding],
        fix_instructions: Optional[str],
    ) -> str:
        """Подготовить задачу для следующей итерации Copilot"""
        
        findings_text = "\n".join([
            f"- {f.severity}: {f.description}" + (f" ({f.location})" if f.location else "")
            for f in findings
            if f.severity in ("CRITICAL", "HIGH", "MEDIUM")  # LOW пропускаем
        ])
        
        task = f"""ИСПРАВЬ НАЙДЕННЫЕ ПРОБЛЕМЫ:

Оригинальная задача: {original_task}

Code review нашёл следующие проблемы:
{findings_text}

{f"Инструкции: {fix_instructions}" if fix_instructions else ""}

Исправь эти проблемы. После исправления код снова будет проверен."""
        
        return task
