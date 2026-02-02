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
from .log_filter import LogFilter
from .log_watcher import LogWatcher, AnalysisResult
from .glm_client import clean_surrogates

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
    had_changes: bool = False  # Были ли изменения в git


@dataclass
class ReviewLoopResult:
    """Финальный результат review loop"""
    success: bool
    iterations: int
    total_findings: int
    fixed_findings: int
    remaining_findings: List[Finding] = field(default_factory=list)
    history: List[LoopIteration] = field(default_factory=list)
    cycle_detected: bool = False
    cycle_reason: str = ""


# Промпт для анализа ситуации и принятия решения
SITUATION_ANALYSIS_PROMPT = """Ты - умный помощник Bender. Проанализируй текущую ситуацию и прими решение.

ЗАДАЧА: {task}

ТЕКУЩАЯ СИТУАЦИЯ:
{situation}

ПОСЛЕДНИЙ ВЫВОД (последние 2000 символов):
{output}

Проанализируй и реши что делать. Возможные действия:
- retry: попробовать ещё раз (если временная ошибка типа 403, 429, timeout)
- wait: подождать N секунд и попробовать (если rate limit)  
- continue: продолжить работу (если всё идёт нормально)
- switch_model: попробовать другую модель (если текущая не справляется)
- abort: прекратить (если ошибка критическая и неисправимая)
- ask_user: спросить пользователя что делать

Ответь JSON:
{{
    "action": "retry" | "wait" | "continue" | "switch_model" | "abort" | "ask_user",
    "reason": "краткое объяснение почему",
    "wait_seconds": 30,  // если action=wait
    "message": "сообщение для пользователя"  // если action=ask_user
}}

ТОЛЬКО JSON."""


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

ВАЖНО:
- Будь дотошным, но НЕ придумывай ошибки ради галочки
- НЕ пиши про мелкий code style / форматирование / "можно улучшить"
- Только РЕАЛЬНЫЕ проблемы которые нужно исправить
- Ты ТОЛЬКО НАХОДИШЬ ошибки, НЕ ИСПРАВЛЯЙ их — copilot исправит

Выведи findings в формате:
- CRITICAL/HIGH/MEDIUM/LOW: описание проблемы. файл:строка

Если проблем нет — напиши "Проблем не найдено"."""


class ReviewLoopManager:
    """Менеджер итеративного цикла review"""
    
    MAX_ITERATIONS = 10
    
    def __init__(
        self,
        llm: LLMRouter,
        manager_config: ManagerConfig,
        on_status: Optional[Callable[[str], Awaitable[None]]] = None,
        on_question: Optional[Callable[[str], Awaitable[Optional[str]]]] = None,
        use_copilot_reviewer: bool = False,
        skip_llm: bool = False,  # Пропустить LLM анализ (simple mode)
        use_droid_mode: bool = False,  # Использовать droid для execution И review
        skip_first_execution: bool = False,  # Пропустить первое выполнение, сразу к ревью
    ):
        self.llm = llm
        self.config = manager_config
        self.on_status = on_status
        self.on_question = on_question
        self.use_copilot_reviewer = use_copilot_reviewer
        self.skip_llm = skip_llm
        self.use_droid_mode = use_droid_mode
        self.skip_first_execution = skip_first_execution
        self.history: List[LoopIteration] = []
        self._stop_requested = False
        
        # Умный анализ логов
        self.log_filter = LogFilter()
        self.log_watcher = LogWatcher(llm, self.log_filter)
    
    @property
    def reviewer_type(self) -> WorkerType:
        """Какой воркер используем для review"""
        if self.use_droid_mode:
            return WorkerType.DROID
        return WorkerType.OPUS if self.use_copilot_reviewer else WorkerType.CODEX
    
    @property
    def reviewer_name(self) -> str:
        if self.use_droid_mode:
            return "droid"
        return "copilot" if self.use_copilot_reviewer else "codex"
    
    def request_stop(self) -> None:
        """Запросить остановку"""
        self._stop_requested = True
    
    async def _report(self, message: str) -> None:
        """Отправить статус"""
        logger.info(f"[ReviewLoop] {message}")
        if self.on_status:
            await self.on_status(f"[Loop] {message}")
    
    async def _check_git_changes(self) -> bool:
        """Проверить были ли изменения в git после последней итерации
        
        Смотрим git status - если есть modified/added/deleted файлы, значит были изменения.
        """
        try:
            import asyncio
            proc = await asyncio.create_subprocess_exec(
                "git", "status", "--porcelain",
                cwd=str(self.config.project_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            # Если есть вывод - есть изменения
            output = stdout.decode().strip()
            if output:
                logger.info(f"[ReviewLoop] Git changes detected: {len(output.splitlines())} files")
                return True
            return False
        except Exception as e:
            logger.warning(f"[ReviewLoop] Failed to check git status: {e}")
            return False
    
    async def _clarify_task(self, task: str, skip_llm: bool = False) -> Optional[ClarifiedTask]:
        """Уточнить задачу через GLM
        
        Args:
            task: Задача
            skip_llm: Пропустить LLM анализ (вернуть None сразу)
        """
        if skip_llm:
            logger.info("[ReviewLoop] Skipping LLM clarification (simple mode)")
            return None
        
        try:
            clarifier = TaskClarifier(
                llm=self.llm,
                project_path=self.config.project_path,
                on_ask_user=self.on_question,  # Передаём callback для вопросов
            )
            return await clarifier.clarify(task)
        except Exception as e:
            logger.warning(f"[ReviewLoop] Failed to clarify task: {e}")
            return None
    
    def _format_task_with_criteria(self, clarified: ClarifiedTask) -> str:
        """Форматировать задачу с критериями для Copilot"""
        # Если критериев нет - отправляем задачу как есть
        if not clarified.acceptance_criteria:
            return clarified.clarified_task
        
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
    
    async def _analyze_situation(self, task: str, situation: str, output: str) -> dict:
        """Умный анализ ситуации через LLM
        
        Вызывается когда что-то идёт не так (ошибка, таймаут, etc.)
        LLM принимает решение что делать дальше.
        
        Returns:
            dict с action, reason, и доп параметрами
        """
        # Если skip_llm - возвращаем дефолтное решение
        if self.skip_llm:
            # Простая логика без LLM
            output_lower = output.lower()
            if "error: 403" in output_lower or "error: 429" in output_lower:
                return {"action": "wait", "reason": "Rate limit detected", "wait_seconds": 30}
            elif "timeout" in output_lower or "connection" in output_lower:
                return {"action": "retry", "reason": "Network error"}
            else:
                return {"action": "continue", "reason": "No critical errors detected"}
        
        # Используем LLM для анализа
        try:
            prompt = SITUATION_ANALYSIS_PROMPT.format(
                task=task[:500],
                situation=situation,
                output=output[-2000:] if len(output) > 2000 else output
            )
            
            response = await self.llm.generate(prompt, temperature=0.2, json_mode=False)
            
            # Парсим JSON ответ
            import json
            import re
            
            # Ищем JSON в ответе
            json_match = re.search(r'\{[^{}]*\}', response, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
                await self._report(f"🤖 LLM decision: {result.get('action', 'unknown')} - {result.get('reason', '')}")
                return result
            else:
                logger.warning(f"[ReviewLoop] Could not parse LLM response: {response[:200]}")
                return {"action": "continue", "reason": "Could not parse LLM response"}
                
        except Exception as e:
            logger.warning(f"[ReviewLoop] LLM analysis failed: {e}")
            return {"action": "continue", "reason": f"LLM error: {e}"}
    
    def _detect_cycle(self) -> tuple:
        """Детекция бесконечного цикла
        
        Проверяет последние 3 итерации на повторяющиеся ошибки.
        Цикл = одни и те же ошибки повторяются, copilot не может их исправить.
        
        Returns:
            (is_cycle, reason, repeating_issues)
        """
        if len(self.history) < 3:
            return False, "", []
        
        # Берём последние 3 итерации
        last_3 = self.history[-3:]
        
        # Собираем все findings как множества текстов ошибок
        findings_sets = []
        for iteration in last_3:
            # Используем полный текст description для сравнения
            findings_key = frozenset(
                f.description.strip().lower()
                for f in iteration.findings
            )
            findings_sets.append(findings_key)
        
        # Ищем ошибки которые повторяются во всех 3 итерациях
        if all(findings_sets):
            common_errors = findings_sets[0]
            for fs in findings_sets[1:]:
                common_errors = common_errors & fs
            
            if common_errors:
                # Есть повторяющиеся ошибки - это цикл
                repeating = list(common_errors)[:5]  # Показываем до 5
                return True, f"{len(common_errors)} issues keep repeating", repeating
        
        # Если все 3 итерации имеют абсолютно одинаковые findings
        if len(set(map(tuple, [sorted(fs) for fs in findings_sets]))) == 1 and findings_sets[0]:
            repeating = [f.description for f in last_3[0].findings[:5]]
            return True, f"Same {len(last_3[0].findings)} issues repeated 3 times", repeating
        
        return False, "", []
    
    def _get_context_from_history(self, last_n: int = 3) -> str:
        """Получить контекст из последних N итераций
        
        Передаётся в задачу чтобы AI знал что уже пробовали.
        """
        if not self.history:
            return ""
        
        context_parts = []
        for iteration in self.history[-last_n:]:
            findings_str = ", ".join(
                f"{f.severity}: {f.description[:50]}" 
                for f in iteration.findings[:5]
            )
            context_parts.append(
                f"Iteration {iteration.iteration}: "
                f"{len(iteration.findings)} issues ({findings_str}), "
                f"decision: {iteration.decision.value if iteration.decision else 'unknown'}"
            )
        
        return "\n".join(context_parts)
    
    async def run_loop(
        self,
        task: str,
        max_iterations: Optional[int] = None,
        skip_llm_analysis: bool = False,
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
        
        # 0. Анализ и уточнение задачи через GLM (если не skip)
        if skip_llm_analysis:
            await self._report("Skipping LLM analysis (simple mode)")
            clarified = None
        else:
            await self._report("Analyzing task with GLM...")
            clarified = await self._clarify_task(task, skip_llm=False)
        
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
            
            # Проверка на цикл
            is_cycle, cycle_reason, repeating_issues = self._detect_cycle()
            if is_cycle:
                await self._report(f"⚠️ Cycle detected: {cycle_reason}")
                # Показываем какие именно ошибки не решаются
                if repeating_issues:
                    await self._report("🔄 Нерешаемые проблемы:")
                    for issue in repeating_issues[:5]:
                        issue_short = issue[:100] + "..." if len(issue) > 100 else issue
                        await self._report(f"   • {issue_short}")
                return ReviewLoopResult(
                    success=False,
                    iterations=iteration_num - 1,
                    total_findings=total_findings,
                    fixed_findings=fixed_findings,
                    remaining_findings=self.history[-1].findings if self.history else [],
                    history=self.history,
                    cycle_detected=True,
                    cycle_reason=cycle_reason,
                )
            
            await self._report(f"=== Iteration {iteration_num}/{max_iter} ===")
            
            # Добавляем контекст предыдущих итераций
            history_context = self._get_context_from_history(3)
            task_with_context = current_task
            if history_context:
                task_with_context = f"{current_task}\n\n📋 Previous iterations:\n{history_context}"
            
            # 1. Запустить worker (droid или copilot) — пропускаем если skip_first_execution на первой итерации
            skip_execution = self.skip_first_execution and iteration_num == 1
            if skip_execution:
                await self._report("Review-first mode: skipping execution, going straight to review")
                copilot_output = ""
            else:
                execution_type = WorkerType.DROID if self.use_droid_mode else WorkerType.OPUS
                execution_name = "droid" if self.use_droid_mode else "copilot"
                await self._report(f"Running {execution_name} with task...")
                copilot_output = await self._run_worker(
                    execution_type, 
                    task_with_context,
                    f"{execution_name}-iter-{iteration_num}"
                )
                # Краткий результат работы copilot
                if copilot_output:
                    await self._summarize_worker_output(execution_name, copilot_output)
            
            if self._stop_requested:
                break
            
            # 1.5 Проверить были ли изменения в git ПОСЛЕ execution
            had_changes = await self._check_git_changes()
            if had_changes:
                await self._report("📝 Changes detected in repository")
            
            # 2. Запустить review (droid, copilot или codex)
            await self._report(f"Running {self.reviewer_name} review...")
            review_task = REVIEW_TASK.format(
                context=task,
                criteria=self._format_criteria(clarified) if clarified else "Нет критериев"
            )
            # Добавляем историю в review тоже
            if history_context:
                review_task += f"\n\n📋 Previous iterations (avoid repeating same fixes):\n{history_context}"
            
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
                had_changes=had_changes,  # из проверки после execution
            )
            
            await self._report(f"Found {len(findings)} issues" + (", had changes" if had_changes else ", no changes"))
            
            # Выводим конкретные проблемы в терминал
            for finding in findings[:5]:  # Максимум 5, чтобы не засорять
                severity_emoji = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🟢"}.get(finding.severity, "⚪")
                loc = f" [{finding.location}]" if finding.location else ""
                desc = finding.description[:80] + "..." if len(finding.description) > 80 else finding.description
                await self._report(f"  {severity_emoji} {finding.severity}: {desc}{loc}")
            if len(findings) > 5:
                await self._report(f"  ... и ещё {len(findings) - 5} проблем")
            
            # 4. Спросить GLM что делать (или решить без GLM в simple mode)
            decision, fix_instructions = await self._analyze_findings(
                task, findings, iteration_num, max_iter, skip_llm=skip_llm_analysis,
                had_changes=had_changes
            )
            
            iteration.decision = decision
            iteration.fix_instructions = fix_instructions
            self.history.append(iteration)
            
            if skip_llm_analysis:
                await self._report(f"Decision (simple mode): {decision.value}")
            else:
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
        
        # Создаём LLM analyze callback для codex
        async def llm_analyze_callback(log: str, task_text: str, elapsed: float) -> dict:
            """LLM анализирует лог и решает статус"""
            try:
                analysis = await self.log_watcher.analyze(log, task_text, elapsed)
                return {
                    "status": analysis.result.value,
                    "summary": analysis.summary,
                    "suggestion": analysis.suggestion,
                }
            except Exception as e:
                logger.debug(f"LLM analyze error: {e}")
                return {"status": "working", "summary": "Анализ недоступен"}
        
        # Создаём новый worker для каждой задачи
        worker_manager = WorkerManager(
            config=self.config,
            on_output=None,
            on_status=self.on_status,
            on_question=self.on_question,
            llm_analyze=llm_analyze_callback if not self.skip_llm else None,
        )
        
        max_retries = 3
        retry_delay = 10  # секунд между retry

        # Имя воркера для логов/статуса (чтобы не потерять при exception)
        if worker_type == WorkerType.DROID:
            worker_name = "droid"
        elif worker_type == WorkerType.OPUS:
            worker_name = "copilot"
        else:
            worker_name = "codex"
        
        for attempt in range(max_retries):
            try:
                await worker_manager.start_task(task, worker_type)
                start_time = asyncio.get_event_loop().time()
                
                # Умный статус каждую минуту через LogWatcher
                async def report_status():
                    last_report = start_time
                    while True:
                        await asyncio.sleep(10)
                        now = asyncio.get_event_loop().time()
                        # Увеличен интервал до 120s чтобы не упираться в rate limit
                        if now - last_report >= 120:
                            elapsed = int(now - start_time)
                            
                            # Получаем текущий лог
                            output = await worker_manager.get_output()
                            if output and len(output) > 100:
                                # Сначала пробуем без GLM - ищем прогресс в логе
                                import re
                                # Полная очистка ANSI/terminal escape sequences
                                clean_output = re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]', '', output)  # CSI sequences
                                clean_output = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?', '', clean_output)  # OSC sequences
                                clean_output = re.sub(r'\x1b[=>]', '', clean_output)  # Mode switches
                                clean_output = re.sub(r'\x1b\([A-Z0-9]', '', clean_output)  # Charset switches
                                clean_output = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', clean_output)  # Control chars
                                
                                # Ищем признаки прогресса
                                progress_patterns = [
                                    r'Updated:.*total.*completed',
                                    r'Created|Writing|Editing|Adding',
                                    r'✓|completed|success',
                                    r'\.tsx|\.ts|\.html|\.js|\.py',
                                ]
                                progress_found = any(re.search(p, clean_output[-3000:], re.IGNORECASE) for p in progress_patterns)
                                
                                if progress_found:
                                    # Показать прогресс без GLM
                                    # Ищем последнюю строку с "Updated:" или файлом
                                    for line in reversed(clean_output.split('\n')):
                                        line = line.strip()
                                        if 'Updated:' in line or re.search(r'\.(tsx|ts|html|js|py)', line):
                                            await self._report(f"⏳ [{elapsed}s] {line[:70]}")
                                            break
                                    else:
                                        await self._report(f"⏳ [{elapsed}s] {worker_name} работает...")
                                    last_report = now
                                    continue
                                
                                # Если нет прогресса — пробуем GLM (если не simple mode)
                                if not self.skip_llm:
                                    try:
                                        analysis = await self.log_watcher.analyze(output, task, elapsed)
                                        summary = analysis.summary
                                        # Выводим полный статус на нескольких строках
                                        await self._report(f"⏳ [{elapsed}s] Статус:")
                                        # Разбиваем summary на строки по ~80 символов
                                        words = summary.split()
                                        lines = []
                                        current_line = "   "
                                        for word in words:
                                            if len(current_line) + len(word) + 1 > 80:
                                                lines.append(current_line)
                                                current_line = "   " + word
                                            else:
                                                current_line += " " + word if current_line != "   " else word
                                        if current_line.strip():
                                            lines.append(current_line)
                                        for line in lines[:4]:  # Max 4 lines
                                            await self._report(line)
                                        last_report = now
                                        continue
                                    except Exception as e:
                                        logger.warning(f"LogWatcher failed: {e}")
                                
                                # Fallback: показываем последнюю значимую строку лога
                                # Исключаем TUI-мусор и escape-последовательности
                                lines = []
                                for l in clean_output.split('\n'):
                                    l = l.strip()
                                    if not l or len(l) < 10:
                                        continue
                                    # Исключаем TUI-мусор
                                    skip_patterns = ['? for help', 'shift+tab', 'ctrl+', '╭', '╮', '╰', '╯', '│', '─',
                                                     '[?', '[>', 'c]', '�', 'Tip:', '/model', '/experimental']
                                    if any(p in l.lower() or p in l for p in skip_patterns):
                                        continue
                                    # Исключаем строки с box drawing или спецсимволами
                                    if re.match(r'^[╭╮╰╯│─\s]+$', l):
                                        continue
                                    # Оставляем только осмысленные строки
                                    if len(l) > 20 and not l.startswith('[') and not re.match(r'^[\s\W]+$', l):
                                        lines.append(l)
                                
                                if lines:
                                    # Ищем строку с действием (Read, Search, Exploring и т.д.)
                                    action_line = None
                                    for line in reversed(lines[-20:]):
                                        if any(kw in line for kw in ['Read', 'Search', 'Exploring', 'Writing', 'Creating', 'Analyzing', 'Checking']):
                                            action_line = line[:60]
                                            break
                                    if action_line:
                                        await self._report(f"⏳ [{elapsed}s] {action_line}")
                                    else:
                                        await self._report(f"⏳ [{elapsed}s] {worker_name} анализирует...")
                                else:
                                    await self._report(f"⏳ [{elapsed}s] {worker_name} работает...")
                            else:
                                await self._report(f"⏳ [{elapsed}s] {worker_name} запускается...")
                            
                            last_report = now
                
                status_task = asyncio.create_task(report_status())
                try:
                    success, output = await worker_manager.wait_for_completion(timeout=1800)
                finally:
                    status_task.cancel()
                    try:
                        await status_task
                    except asyncio.CancelledError:
                        pass
                
                # Если успех - возвращаем
                if success:
                    await worker_manager.stop()
                    return output
                
                # Ошибка - анализируем ситуацию через LLM
                situation = f"Worker {worker_name} вернул ошибку на попытке {attempt + 1}/{max_retries}"
                decision = await self._analyze_situation(task, situation, output)
                
                action = decision.get("action", "continue")
                reason = decision.get("reason", "")
                
                await worker_manager.stop()
                
                if action == "retry":
                    # Проверяем лимит retry
                    if attempt >= max_retries - 1:
                        await self._report(f"❌ {worker_name} failed after {max_retries} retry attempts: {reason}")
                        return output
                    await self._report(f"🔄 {worker_name}: {reason} - retrying ({attempt + 1}/{max_retries})...")
                    await asyncio.sleep(5)
                    
                elif action == "wait":
                    # Проверяем лимит retry
                    if attempt >= max_retries - 1:
                        await self._report(f"❌ {worker_name} failed after {max_retries} wait attempts: {reason}")
                        return output
                    wait_secs = decision.get("wait_seconds", 30)
                    await self._report(f"⏳ {worker_name}: {reason} - waiting {wait_secs}s ({attempt + 1}/{max_retries})...")
                    await asyncio.sleep(wait_secs)
                    
                elif action == "abort":
                    await self._report(f"❌ {worker_name}: {reason} - aborting")
                    return output
                    
                elif action == "ask_user":
                    msg = decision.get("message", "Что делать дальше?")
                    await self._report(f"❓ {worker_name}: {msg}")
                    # TODO: реально спросить пользователя
                    return output
                    
                else:  # continue или unknown
                    if attempt >= max_retries - 1:
                        await self._report(f"❌ {worker_name} failed after {max_retries} attempts: {reason}")
                        return output
                    await asyncio.sleep(10)
                    
            except Exception as e:
                await self._report(f"⚠️ {worker_name} exception: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(10)
                else:
                    raise
        
        return output if 'output' in dir() else ""
        
    async def _cleanup_worker(self, worker_manager) -> None:
        """Cleanup helper"""
        try:
            await worker_manager.stop()
        except Exception:
            pass
    
    async def cleanup(self) -> None:
        """Очистка после завершения цикла"""
        pass  # Ничего не нужно - каждый worker создаётся и удаляется отдельно
    
    async def _summarize_worker_output(self, worker_name: str, output: str) -> None:
        """Вывести краткий результат работы worker'а"""
        import re
        
        # Очистка от ANSI
        clean = re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]', '', output)
        clean = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?', '', clean)
        
        # Ищем ключевые индикаторы
        lines = clean.split('\n')
        
        # Собираем статистику
        created = len(re.findall(r'(?:Created|Создан[оа]?)\s+\S+', clean, re.IGNORECASE))
        updated = len(re.findall(r'(?:Updated|Обновлен[оа]?|Изменен[оа]?)\s+\S+', clean, re.IGNORECASE))
        
        # Ищем файлы
        files = set(re.findall(r'[\w/.-]+\.(?:tsx?|jsx?|py|html|css|json|md|yaml|yml)', clean))
        
        # Формируем краткий результат
        parts = []
        if created:
            parts.append(f"создано {created}")
        if updated:
            parts.append(f"изменено {updated}")
        if files:
            parts.append(f"файлов: {len(files)}")
        
        if parts:
            await self._report(f"  📊 {worker_name} результат: {', '.join(parts)}")
        
        # Показываем последние значимые строки (не пустые, не TUI-мусор)
        significant = []
        for line in reversed(lines[-50:]):
            line = line.strip()
            if line and len(line) > 10 and not line.startswith(('─', '│', '╭', '╰', '┌', '└')):
                if any(kw in line.lower() for kw in ['complete', 'done', 'success', 'error', 'fail', 'created', 'updated', 'ready']):
                    significant.append(line[:80])
                    if len(significant) >= 2:
                        break
        
        for line in reversed(significant):
            await self._report(f"  → {line}")
    
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
        skip_llm: bool = False,
        had_changes: bool = False,
    ) -> tuple[LoopDecision, Optional[str]]:
        """Спросить GLM что делать с findings (или решить без GLM если skip_llm)
        
        Логика: 
        - Если были изменения → продолжать искать ошибки (даже если findings пуст)
        - DONE только если: нет findings И не было изменений
        """
        
        # Если были изменения, продолжаем даже без findings
        if not findings:
            if had_changes:
                logger.info("[ReviewLoop] No findings but changes detected - continue reviewing")
                return LoopDecision.FIX, "Changes detected, verify they work correctly"
            return LoopDecision.DONE, None
        
        # Simple mode — без GLM, решаем по severity
        if skip_llm or self.skip_llm:
            logger.info("[ReviewLoop] Simple mode - analyzing findings without LLM")
            critical_count = sum(1 for f in findings if f.severity == "CRITICAL")
            high_count = sum(1 for f in findings if f.severity == "HIGH")
            medium_count = sum(1 for f in findings if f.severity == "MEDIUM")
            
            if critical_count > 0:
                return LoopDecision.FIX, f"Fix {critical_count} CRITICAL issues"
            elif high_count > 0:
                return LoopDecision.FIX, f"Fix {high_count} HIGH severity issues"
            elif medium_count > 0 and iteration < max_iterations - 2:
                return LoopDecision.FIX, f"Fix {medium_count} MEDIUM severity issues"
            else:
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
