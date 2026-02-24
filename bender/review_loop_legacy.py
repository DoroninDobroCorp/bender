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
import json
import logging
import re
import tempfile
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
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
    
    def to_dict(self) -> dict:
        return {"severity": self.severity, "description": self.description, "location": self.location}
    
    @classmethod
    def from_dict(cls, d: dict) -> "Finding":
        return cls(severity=d.get("severity", "MEDIUM"), description=d.get("description", ""), location=d.get("location"))


class FindingsStore:
    """Надёжное хранение findings с резервированием"""
    
    def __init__(self, session_id: str):
        self.session_id = session_id
        # Сохраняем в папку проекта, а не в /tmp
        self._dir = Path.cwd() / ".bender-cache"
        self._dir.mkdir(exist_ok=True)
        
        self._file = self._dir / f"findings-{session_id}.json"
        self._backup_file = self._file.with_suffix('.json.bak')
        
    def save(self, findings: List[Finding], raw_output: str, iteration: int) -> None:
        """Сохранить findings и ПОЛНЫЙ raw output (без обрезки!)"""
        data = {
            "iteration": iteration,
            "findings": [f.to_dict() for f in findings],
            "findings_count": len(findings),
            "raw_output": raw_output,  # ПОЛНЫЙ текст, не preview!
            "timestamp": datetime.now().isoformat(),
        }
        
        json_data = json.dumps(data, ensure_ascii=False, indent=2)
        
        # 1. Сохраняем бэкап
        try:
            self._backup_file.write_text(json_data, encoding='utf-8')
        except Exception:
            pass
            
        # 2. Сохраняем основной файл
        try:
            self._file.write_text(json_data, encoding='utf-8')
            logger.info(f"[FindingsStore] Saved {len(findings)} findings to {self._file.name}")
        except Exception as e:
            logger.error(f"[FindingsStore] Save failed: {e}")

    def load(self) -> tuple[List[Finding], str, int]:
        """Загрузить findings и ПОЛНЫЙ raw output"""
        # Пытаемся загрузить из основного, затем из бэкапа
        for fpath in [self._file, self._backup_file]:
            if not fpath.exists():
                continue
            try:
                data = json.loads(fpath.read_text(encoding='utf-8'))
                findings = [Finding.from_dict(f) for f in data.get("findings", [])]
                # Поддерживаем старый формат (raw_output_preview) и новый (raw_output)
                raw = data.get("raw_output") or data.get("raw_output_preview", "")
                iteration = data.get("iteration", 0)
                logger.info(f"[FindingsStore] Loaded {len(findings)} findings from {fpath.name}")
                return findings, raw, iteration
            except Exception as e:
                logger.warning(f"[FindingsStore] Load failed from {fpath.name}: {e}")
        
        return [], "", 0
        
    def get_for_fix_task(self) -> tuple[str, bool]:
        findings, raw_output, _ = self.load()
        
        if findings:
            lines = [f"- {f.severity}: {f.description}" + (f" [{f.location}]" if f.location else "") for f in findings]
            return "\n".join(lines), True
            
        # Fallback: если findings пустые, но есть raw output
        if raw_output:
            return raw_output, False
            
        return "", False
    
    def cleanup(self) -> None:
        """Удалить файл"""
        if self._file.exists():
            try:
                self._file.unlink()
                logger.debug(f"[FindingsStore] Deleted {self._file.name}")
            except Exception:
                pass


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

ПРАВИЛА SEVERITY:
- CRITICAL: Критические баги, крэши, security дыры — ОБЯЗАТЕЛЬНО исправить
- HIGH: Серьёзные проблемы, неправильная логика — ОБЯЗАТЕЛЬНО исправить
- MEDIUM: Умеренные проблемы, можно работать но желательно исправить
- LOW: Мелкие замечания, стиль, "nice to have" — БУДУТ ПРОИГНОРИРОВАНЫ!

⚠️ Если нет проблем уровня CRITICAL/HIGH/MEDIUM — напиши "Проблем не найдено"
⚠️ НЕ используй LOW для настоящих багов!

Выведи findings в формате:
- CRITICAL/HIGH/MEDIUM/LOW: описание проблемы. файл:строка

Если проблем нет — напиши "Проблем не найдено"."""


# BMAD Party Review — 10-ролевая оценка с scoring
REVIEW_TASK_PARTY = """Проведи BMAD Party Review кода — оценку от 10 ролей:

Контекст задачи: {context}

Критерии приёмки:
{criteria}

## ФАЗА 1: Реальные проверки

ОБЯЗАТЕЛЬНО запусти РЕАЛЬНЫЕ инструменты перед оценкой:
```bash
pytest tests/ -v --tb=short 2>&1 | tail -30
mypy backend/ --strict 2>&1 | tail -20
ruff check backend/ 2>&1 | tail -20
```

## ФАЗА 2: Оценки от 10 BMAD ролей

Для КАЖДОЙ роли выведи:
```
=== ROLE: [Имя] ([Роль]) ===
Score: [0-100]
Status: [APPROVED / NEEDS_WORK]
Strengths:
  - ✅ [что хорошо]
Issues:
  - ❌ [проблема] (HIGH: -10)
  - ⚠️ [проблема] (MEDIUM: -5)
  - 💡 [замечание] (LOW: -2)
```

### 10 ролей для оценки:

1. 📊 Mary (Business Analyst) — AC выполнены? Business value? Requirements?
2. 🏗️ Winston (Architect) — Архитектура? Patterns? Scalability? Layer separation?
3. 💻 Amelia (Developer) — Код? TDD? Type hints? Best practices? PEP 8?
4. 📋 John (Product Manager) — Scope? User stories покрыты? Нет scope creep?
5. 🚀 Barry (Quick Flow Solo Dev) — Efficient? No bloat? Lean? Ship-ready?
6. 🧪 Quinn (QA Engineer) — Тесты? Coverage >80%? Edge cases? Integration tests?
7. 🏃 Bob (Scrum Master) — DoD? Process? Story structure? Sprint goals?
8. 📚 Paige (Technical Writer) — Docs? README? API docs? Docstrings?
9. 🎨 Sally (UX Designer) — UX? Accessibility? Error messages? User experience?
10. 🧙 BMad Master — BMAD compliance? Workflow? Все артефакты на месте?

## ФАЗА 3: Итог

```
=== BMAD PARTY SUMMARY ===
Scores: Mary=[X] Winston=[X] Amelia=[X] John=[X] Barry=[X] Quinn=[X] Bob=[X] Paige=[X] Sally=[X] BMad=[X]
Average: [X]/100
Min: [X]/100
Agents below 98: [N]
Status: [APPROVED 98+ / NEEDS_WORK]
```

## Правила severity для findings:
- HIGH: -10 баллов, блокирует approval → ОБЯЗАТЕЛЬНО исправить
- MEDIUM: -5 баллов → желательно исправить
- LOW: -2 балла → nice to have

Выведи findings в формате:
- CRITICAL/HIGH/MEDIUM/LOW: описание проблемы. файл:строка

⚠️ НЕ завышай оценки! Реальные тесты, реальный coverage, реальные проверки.
⚠️ Если pytest/mypy/ruff показали ошибки — это МИНИМУМ HIGH."""


# Промпт: LLM решает продолжать или нет
# ВАЖНО: Явное правило — если нет CRITICAL/HIGH/MEDIUM проблем = ЗАВЕРШИТЬ!
SHOULD_CONTINUE_PROMPT = """Проанализируй вывод code reviewer и реши: нужно ли ИСПРАВЛЯТЬ код?

Вывод reviewer:
```
{review_output}
```

ПРАВИЛА ПРИОРИТЕТА SEVERITY:
1. CRITICAL/HIGH = ОБЯЗАТЕЛЬНО исправить (continue=TRUE)
2. MEDIUM = Желательно исправить если есть (continue=TRUE)
3. LOW = ИГНОРИРОВАТЬ! НЕ считается проблемой (continue=FALSE)

КОГДА continue=TRUE:
- Есть хотя бы одна проблема уровня CRITICAL, HIGH или MEDIUM
- Найдены реальные баги, крэши, security уязвимости
- Код не работает как ожидается

КОГДА continue=FALSE (ЗАВЕРШИТЬ ЦИКЛ!):
- Reviewer написал "проблем не найдено", "всё хорошо", "можно мержить"
- Есть ТОЛЬКО проблемы уровня LOW (они игнорируются!)
- Только мелкие замечания про стиль/форматирование
- Только suggestions "можно улучшить", "рекомендуется"
- Код работает правильно

⚠️ ВАЖНО: Если ВСЕ найденные проблемы имеют severity=LOW → continue=FALSE!
⚠️ ВАЖНО: LOW проблемы НЕ являются причиной для продолжения цикла!

Ответь СТРОГО JSON:
{{"continue": true/false, "reason": "причина (1 предложение)", "has_medium_plus": true/false}}

ТОЛЬКО JSON!"""


# Промпт: LLM извлекает структурированные findings из вывода reviewer
EXTRACT_FINDINGS_PROMPT = """Проанализируй вывод code reviewer и извлеки список проблем (findings).

Вывод reviewer:
```
{review_output}
```

Извлеки ВСЕ найденные проблемы. Для каждой укажи:
- severity: CRITICAL, HIGH, MEDIUM или LOW
- description: краткое описание проблемы
- location: файл:строка (или null если неизвестно)

Ответь СТРОГО JSON:
{{"has_issues": true/false, "summary": "краткое резюме (1 предложение)", "findings": [{{"severity": "HIGH", "description": "описание", "location": "file.py:42"}}]}}

Если проблем нет — верни {{"has_issues": false, "summary": "Проблем не найдено", "findings": []}}

ТОЛЬКО JSON!"""


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
        use_droid_exec: bool = False,  # Использовать droid для execution (-d)
        use_droid_review: bool = False,  # Использовать droid для review тоже (-dd)
        use_party_mode: bool = False,  # BMAD Party 10-ролевая оценка (-P)
        skip_first_execution: bool = False,  # Пропустить первое выполнение, сразу к ревью
        use_streaming: bool = False,  # Использовать event_stream() вместо wait_for_completion()
        # Legacy support
        use_droid_mode: bool = False,  # deprecated, use use_droid_exec
    ):
        self.llm = llm
        self.config = manager_config
        self.on_status = on_status
        self.on_question = on_question
        self.use_copilot_reviewer = use_copilot_reviewer
        self.skip_llm = skip_llm
        # Support both old and new API
        self.use_droid_exec = use_droid_exec or use_droid_mode
        self.use_droid_review = use_droid_review
        self.use_party_mode = use_party_mode
        self.skip_first_execution = skip_first_execution
        self.use_streaming = use_streaming
        self.history: List[LoopIteration] = []
        self._stop_requested = False

        # Умный анализ логов
        self.log_filter = LogFilter()
        self.log_watcher = LogWatcher(llm, self.log_filter)

        # Надёжное хранилище findings между итерациями
        # Инициализируем FindingsStore по session id (используем временный файл)
        self._session_id = uuid.uuid4().hex[:8]
        self._findings_store = FindingsStore(self._session_id)
    
    @property
    def reviewer_type(self) -> WorkerType:
        """Какой воркер используем для review
        
        -dd флаг включает droid для review тоже
        """
        if self.use_droid_review:
            return WorkerType.DROID
        return WorkerType.OPUS if self.use_copilot_reviewer else WorkerType.CODEX
    
    @property
    def reviewer_name(self) -> str:
        """Имя reviewer worker"""
        if self.use_droid_review:
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
        """Умное сжатие истории для предотвращения context amnesia
        
        Вместо простой конкатенации всех итераций, используем:
        - Для старых итераций: краткое саммари (1 строка)
        - Для последних N: полный контекст findings
        
        Это предотвращает раздувание контекста и потерю фокуса модели.
        """
        if not self.history:
            return ""
        
        context_parts = []
        
        # 1. Для очень старых итераций (которые не вошли в last_n)
        # Оставляем только ОДНУ строку: "Iteration X: Fixed 3 errors"
        old_history = self.history[:-last_n] if len(self.history) > last_n else []
        if old_history:
            context_parts.append("--- OLD HISTORY (Summarized) ---")
            for iteration in old_history:
                findings_cnt = len(iteration.findings)
                decision_str = iteration.decision.value if iteration.decision else 'unknown'
                context_parts.append(
                    f"Iter {iteration.iteration}: Found {findings_cnt} issues, decision: {decision_str}"
                )
        
        # 2. Для последних last_n итераций даем полный контекст, но без "воды"
        recent_history = self.history[-last_n:] if len(self.history) >= last_n else self.history
        if recent_history:
            context_parts.append(f"\n--- RECENT HISTORY (Last {len(recent_history)}) ---")
            for iteration in recent_history:
                # Берем только критически важное: какие были findings
                if not iteration.findings:
                    context_parts.append(f"Iteration {iteration.iteration}: No issues found")
                    continue
                
                # Ограничиваем до 5 findings на итерацию чтобы не раздувать
                findings_to_show = iteration.findings[:5]
                findings_str = ", ".join(
                    f"{f.severity}: {f.description[:50]}" 
                    for f in findings_to_show
                )
                
                if len(iteration.findings) > 5:
                    findings_str += f", ... (+{len(iteration.findings)-5} more)"
                
                decision_str = iteration.decision.value if iteration.decision else 'unknown'
                context_parts.append(
                    f"Iteration {iteration.iteration}: "
                    f"{len(iteration.findings)} issues ({findings_str}), "
                    f"decision: {decision_str}"
                )
        
        return "\n".join(context_parts)
    
    async def run_loop(
        self,
        task: str,
        max_iterations: Optional[int] = None,
        skip_llm_analysis: bool = False,
        initial_errors: Optional[List[str]] = None,
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

        # Continue mode: seed initial findings if provided
        if initial_errors:
            seeded = self._seed_initial_errors(initial_errors)
            if seeded:
                await self._report(f"Continue mode: loaded {seeded} initial errors")
                current_task = self._prepare_fix_task_from_store(task)
            else:
                await self._report("Continue mode: no valid errors parsed, proceeding normally")
        
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
                # ВАЖНО: Если execution пропущен, то изменений быть не может
                had_changes = False
            else:
                execution_type = WorkerType.DROID if self.use_droid_exec else WorkerType.OPUS
                execution_name = "droid" if self.use_droid_exec else "copilot"
                await self._report(f"Running {execution_name} with task...")
                copilot_output = await self._run_worker(
                    execution_type, 
                    task_with_context,
                    f"{execution_name}-iter-{iteration_num}"
                )
                # Краткий результат работы copilot
                if copilot_output:
                    await self._summarize_worker_output(execution_name, copilot_output)
                
                # 1.5 Проверить были ли изменения в git ПОСЛЕ execution
                had_changes = await self._check_git_changes()
                if had_changes:
                    await self._report("📝 Changes detected in repository")
            
            if self._stop_requested:
                break
            
            # 2. Запустить review (droid, copilot или codex)
            review_mode_name = "BMAD Party" if self.use_party_mode else self.reviewer_name
            await self._report(f"Running {review_mode_name} review...")
            review_template = REVIEW_TASK_PARTY if self.use_party_mode else REVIEW_TASK
            review_task = review_template.format(
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

            # ВАЖНО: Извлекаем человеческий текст из RAW output (может содержать JSON события от droid)
            # Делаем это ДО проверки на пустоту, иначе JSON события будут выглядеть как "пустой вывод"
            human_text = self._extract_human_response(review_output)
            
            # Retry logic: если output подозрительно короткий, пробуем ещё раз (макс 2 retry)
            min_review_chars = 500 if self.use_party_mode else 200
            review_retries = 0
            max_review_retries = 2
            
            while len(human_text.strip()) < min_review_chars and review_retries < max_review_retries:
                review_retries += 1
                await self._report(f"⚠️ Review output too short ({len(human_text)} chars < {min_review_chars}) — retry {review_retries}/{max_review_retries}")
                logger.warning(f"[ReviewLoop] Review output too short ({len(human_text)} chars), retry {review_retries}/{max_review_retries}")
                
                # Используем fallback worker при retry
                retry_worker = self.reviewer_type
                if review_retries >= 2 and self.reviewer_type != WorkerType.CODEX:
                    retry_worker = WorkerType.CODEX
                    await self._report("🔄 Switching to codex for retry")
                elif review_retries >= 2 and self.reviewer_type == WorkerType.CODEX:
                    retry_worker = WorkerType.OPUS
                    await self._report("🔄 Switching to copilot for retry")
                
                review_output = await self._run_worker(
                    retry_worker,
                    review_task,
                    f"{self.reviewer_name}-retry{review_retries}-iter-{iteration_num}"
                )
                human_text = self._extract_human_response(review_output)
            
            # Если вывод reviewer пустой/похоже на промпт — пробуем fallback на codex
            if self._review_output_is_empty(human_text):
                await self._report("⚠️ Reviewer output looks empty — retrying review with codex for full output")
                if self.reviewer_type != WorkerType.CODEX:
                    review_output = await self._run_worker(
                        WorkerType.CODEX,
                        review_task,
                        f"codex-fallback-iter-{iteration_num}"
                    )
                    # Извлекаем текст из fallback тоже
                    human_text = self._extract_human_response(review_output)
            
            # ДИАГНОСТИКА: логируем что получили от worker
            logger.info(f"[ReviewLoop] Got review_output: {len(review_output)} chars")
            # Ищем findings в выводе

            findings_markers = re.findall(r'(CRITICAL|HIGH|MEDIUM|LOW):', review_output, re.IGNORECASE)
            if findings_markers:
                logger.info(f"[ReviewLoop] Found severity markers in output: {findings_markers}")
            else:
                logger.warning("[ReviewLoop] NO severity markers found in review_output!")
                # Логируем длину output без содержимого (может содержать secrets)
                logger.debug("[ReviewLoop] review_output length: %d chars", len(review_output))

            # ДИАГНОСТИКА: сохраняем review_output в файл (только в debug)
            if logger.isEnabledFor(logging.DEBUG):
                try:
                    debug_file = Path(tempfile.gettempdir()) / f"bender-review-output-iter{iteration_num}.txt"
                    debug_file.write_text(review_output)
                    logger.debug("[ReviewLoop] Saved review_output to %s", debug_file)
                except Exception as e:
                    logger.warning("[ReviewLoop] Failed to save review_output: %s", e)
            
            if self._stop_requested:
                break
            
            # 3. ПРОСТОЙ ПОДХОД: LLM решает продолжать или нет
            # Передаём весь вывод reviewer, не парсим findings
            
            # ВАЖНО: Проверяем что review_output содержит реальный ответ, а не только промпт
            # Убираем промпт из начала если он там есть
            clean_review = review_output
            if "🤖 BENDER →" in clean_review:
                # Находим конец промпт-блока (после второй линии ━━━)
                parts = clean_review.split("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
                if len(parts) >= 3:
                    # Берём всё после второго разделителя (там реальный ответ)
                    clean_review = "━━━".join(parts[2:])
                    logger.info(f"[ReviewLoop] Stripped prompt header, remaining: {len(clean_review)} chars")
            
            # Проверяем что осталось что-то значимое (после retries)
            if len(clean_review.strip()) < 100:
                if review_retries >= max_review_retries:
                    logger.error(f"[ReviewLoop] Review output still too short after {review_retries} retries ({len(clean_review)} chars)")
                    await self._report(f"🔴 Reviewer failed after {review_retries} retries — output too short ({len(clean_review)} chars)")
                else:
                    logger.warning(f"[ReviewLoop] Review output too short ({len(clean_review)} chars), may be empty response")
                    await self._report(f"⚠️ Reviewer output is too short - may have failed")
            
            if self.skip_llm:
                # Simple mode - используем regex для парсинга
                findings = self._parse_findings(review_output, iteration_num)
                
                # ВАЖНО: Учитываем severity! LOW игнорируем!
                medium_plus_findings = [f for f in findings if f.severity in ("CRITICAL", "HIGH", "MEDIUM")]
                low_only = len(findings) > 0 and len(medium_plus_findings) == 0
                
                if low_only:
                    should_continue = False
                    reason = f"Only {len(findings)} LOW severity issues (ignored)"
                    logger.info(f"[ReviewLoop] Simple mode: only LOW issues, stopping")
                elif medium_plus_findings:
                    should_continue = True
                    crit = sum(1 for f in findings if f.severity == "CRITICAL")
                    high = sum(1 for f in findings if f.severity == "HIGH")
                    med = sum(1 for f in findings if f.severity == "MEDIUM")
                    reason = f"Found {crit} CRITICAL, {high} HIGH, {med} MEDIUM issues"
                else:
                    should_continue = False
                    reason = "No issues found"
            else:
                # LLM mode - спрашиваем GLM напрямую
                should_continue, reason = await self._should_continue(review_output)
                # LLM extraction (надёжнее regex), fallback на _parse_findings при ошибке
                findings = await self._extract_findings_with_llm(review_output, iteration_num)
                if findings:
                    # Выводим найденные проблемы в терминал для удобного копирования
                    medium_plus = [f for f in findings if f.severity in ("CRITICAL", "HIGH", "MEDIUM")]
                    low_count = len(findings) - len(medium_plus)
                    await self._report(f"📋 Found {len(medium_plus)} MEDIUM+ issues, {low_count} LOW (ignored):")
                    for finding in findings[:10]:  # Показываем до 10
                        severity_emoji = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🟢"}.get(finding.severity, "⚪")
                        loc = f" [{finding.location}]" if finding.location else ""
                        # Помечаем LOW как ignored
                        ignored_mark = " [IGNORED]" if finding.severity == "LOW" else ""
                        await self._report(f"  {severity_emoji} {finding.severity}: {finding.description[:100]}{loc}{ignored_mark}")
            
            iteration = LoopIteration(
                iteration=iteration_num,
                worker=self.reviewer_name,
                findings=findings,
                had_changes=had_changes,
            )
            self.history.append(iteration)
            
            # Выводим решение
            if should_continue:
                await self._report(f"🔄 Continue: {reason}")
                total_findings += len(findings)  # Правильный счётчик
            else:
                await self._report(f"✅ Done: {reason}")
            
            # 4. Принять решение
            if not should_continue:
                if had_changes:
                    await self._report("✅ Review complete - changes look good")
                else:
                    await self._report("✅ Review complete - no more fixes needed")
                return ReviewLoopResult(
                    success=True,
                    iterations=iteration_num,
                    total_findings=total_findings,
                    fixed_findings=fixed_findings,
                    remaining_findings=findings,
                    history=self.history,
                )
            
            # Нужно исправлять - ПРОСТО ПЕРЕДАЁМ ВЕСЬ ВЫВОД REVIEW КАК ЕСТЬ
            fixed_findings += 1
            # Prefer structured findings from FindingsStore; fallback to raw output
            try:
                current_task = self._prepare_fix_task_from_store(task)
            except Exception:
                current_task = self._prepare_fix_task_raw(task, review_output)
            
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
    
    async def _run_worker_stream(
        self,
        worker_type: WorkerType,
        task: str,
        session_suffix: str
    ) -> str:
        """Запустить worker через event_stream() (streaming mode)

        Альтернатива _run_worker() которая:
        - Использует worker.event_stream() вместо wait_for_completion()
        - Публикует events в EventQueue
        - Аккумулирует текст из ITEM_DELTA events
        - Обновляет on_status в реальном времени

        Returns:
            Аккумулированный текст (совместим с legacy API)
        """
        from backend.core.event_queue import EventQueue
        from backend.core.events.schema import EventType

        # Имя воркера
        if worker_type == WorkerType.DROID:
            worker_name = "droid"
        elif worker_type == WorkerType.OPUS:
            worker_name = "copilot"
        else:
            worker_name = "codex"

        # Создаём LLM analyze callback для codex
        async def llm_analyze_callback(log: str, task_text: str, elapsed: float) -> dict:
            try:
                analysis = await self.log_watcher.analyze(log, task_text, elapsed, process_alive=True)
                return {
                    "status": analysis.result.value,
                    "summary": analysis.summary,
                    "suggestion": analysis.suggestion,
                }
            except Exception as e:
                logger.debug(f"LLM analyze error: {e}")
                return {"status": "working", "summary": "Анализ недоступен"}

        # Создаём worker manager
        worker_manager = WorkerManager(
            config=self.config,
            on_output=None,
            on_status=self.on_status,
            on_question=self.on_question,
            llm_analyze=llm_analyze_callback if not self.skip_llm else None,
        )

        # Запускаем задачу
        await worker_manager.start_task(task, worker_type)

        # Получаем worker объект
        worker = worker_manager.current_worker
        if worker is None:
            logger.error(f"[ReviewLoop] Failed to get worker for {worker_name}")
            return ""

        # Аккумуляторы
        accumulated_text = ""
        max_accumulated_size = 50 * 1024 * 1024  # 50 MB limit
        event_queue = EventQueue()
        start_time = asyncio.get_event_loop().time()
        last_status_update = start_time
        status_update_interval = 5.0  # Обновлять статус каждые 5 секунд

        try:
            # Streaming через event_stream()
            logger.info(f"[ReviewLoop] Starting streaming from {worker_name}")
            async for event in worker.event_stream(timeout=1800):
                # Публикуем в EventQueue
                await event_queue.publish(event)

                # Аккумулируем текст из ITEM_DELTA
                if event.event_type == EventType.ITEM_DELTA:
                    text_chunk = event.data.get("text", "")
                    if text_chunk:
                        if len(accumulated_text) + len(text_chunk) >= max_accumulated_size:
                            # Keep last 40MB, discard older text
                            accumulated_text = accumulated_text[-(40 * 1024 * 1024):]
                        accumulated_text += text_chunk

                    # Обновляем on_status буферизованно (не каждый delta)
                    now = asyncio.get_event_loop().time()
                    elapsed = int(now - start_time)
                    if now - last_status_update >= status_update_interval:
                        # Показываем прогресс
                        words = accumulated_text.split()
                        if len(words) > 5:
                            # Берём последние несколько слов для контекста
                            preview = " ".join(words[-10:])[:60]
                            await self._report(f"⏳ [{elapsed}s] {worker_name}: {preview}...")
                        else:
                            await self._report(f"⏳ [{elapsed}s] {worker_name} streaming...")
                        last_status_update = now

                # Логируем важные события
                if event.event_type == EventType.SESSION_START:
                    logger.info(f"[ReviewLoop] {worker_name} session started")
                elif event.event_type == EventType.SESSION_END:
                    logger.info(f"[ReviewLoop] {worker_name} session ended")
                elif event.event_type in (EventType.AGENT_ERROR, EventType.ERROR):
                    logger.warning(f"[ReviewLoop] {worker_name} error event: {event.data}")

            # Stream завершился
            logger.info(f"[ReviewLoop] Streaming complete, accumulated {len(accumulated_text)} chars")

            # Финальный статус
            elapsed = int(asyncio.get_event_loop().time() - start_time)
            await self._report(f"✓ [{elapsed}s] {worker_name} completed")

            await worker_manager.stop()
            return accumulated_text

        except Exception as e:
            logger.error(f"[ReviewLoop] Streaming error: {e}")
            await worker_manager.stop()
            # Fallback: возвращаем что успели накопить
            return accumulated_text

    async def _run_worker(
        self,
        worker_type: WorkerType,
        task: str,
        session_suffix: str
    ) -> str:
        """Запустить worker и дождаться результата (legacy mode или streaming mode)"""

        # Если streaming включён - используем _run_worker_stream
        if self.use_streaming:
            return await self._run_worker_stream(worker_type, task, session_suffix)

        # Legacy mode: wait_for_completion()
        # Создаём LLM analyze callback для codex
        async def llm_analyze_callback(log: str, task_text: str, elapsed: float) -> dict:
            """LLM анализирует лог и решает статус"""
            try:
                # process_alive=True по умолчанию - callback вызывается пока процесс работает
                analysis = await self.log_watcher.analyze(log, task_text, elapsed, process_alive=True)
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
                    last_output_len = 0
                    while True:
                        await asyncio.sleep(10)
                        now = asyncio.get_event_loop().time()
                        elapsed = int(now - start_time)
                        
                        # Получаем текущий лог
                        output = await worker_manager.get_output()
                        output_len = len(output) if output else 0
                        
                        # Подготовка очищенного вывода (если есть)
                        clean_output = ""
                        if output:
                
                            # Полная очистка ANSI/terminal escape sequences
                            clean_output = re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]', '', output)  # CSI sequences
                            clean_output = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?', '', clean_output)  # OSC sequences
                            clean_output = re.sub(r'\x1b[=>]', '', clean_output)  # Mode switches
                            clean_output = re.sub(r'\x1b\([A-Z0-9]', '', clean_output)  # Charset switches
                            clean_output = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', clean_output)  # Control chars
                        
                        # Heartbeat для droid (если тишина)
                        if worker_name == "droid" and output_len == last_output_len and now - last_report >= 30:
                            await self._report(f"⏳ [{elapsed}s] droid still running...")
                            last_report = now
                            continue

                        # Быстрый репорт если лог обновился
                        if output and output_len != last_output_len:
                            last_output_len = output_len
                            # Сначала пробуем без GLM - ищем прогресс в логе
                            
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
                        
                        # Увеличен интервал до 120s чтобы не упираться в rate limit
                        if now - last_report >= 120:
                            if output and output_len > 100:
                                # Если нет прогресса — пробуем GLM (если не simple mode)
                                if not self.skip_llm:
                                    try:
                                        # process_alive=True - мы в цикле пока worker работает
                                        analysis = await self.log_watcher.analyze(output, task, elapsed, process_alive=True)
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
                
                # Если успех - перечитываем output перед возвратом (на случай если лог дописался)
                if success:
                    # Перечитываем финальный вывод перед закрытием
                    final_output = await worker_manager.get_output()
                    if final_output and len(final_output) > len(output):
                        logger.info(f"[ReviewLoop] Got more output on final read: {len(output)} → {len(final_output)}")
                        output = final_output
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
        
        return output
        
    async def _cleanup_worker(self, worker_manager) -> None:
        """Cleanup helper"""
        try:
            await worker_manager.stop()
        except Exception:
            pass
    
    async def cleanup(self) -> None:
        """Очистка после завершения цикла"""
        try:
            self._findings_store.cleanup()
        except Exception:
            pass  # ignore cleanup errors
    
    async def _summarize_worker_output(self, worker_name: str, output: str) -> None:
        """Вывести краткий результат работы worker'а"""

        
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
    
    def _extract_human_response(self, full_output: str) -> str:
        """
        МАКСИМАЛЬНО ТУПАЯ И НАДЕЖНАЯ ОЧИСТКА.
        
        Поддерживает два формата:
        1. Обычный текст (от copilot/codex) - удаляем ANSI и JSON логи в конце
        2. JSON события от droid (stream-json) - извлекаем текст из событий
        """
        if not full_output:
            return ""

        # 1. Удаляем только ANSI цвета (это безопасно)

        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        text = ansi_escape.sub('', full_output)
        
        # 2. Проверяем формат: JSON события или обычный текст?
        # Если больше 3 строк начинаются с {"type": - это JSON события от droid
        lines = text.split('\n')
        json_event_lines = sum(1 for line in lines if line.strip().startswith('{"type":'))
        
        if json_event_lines >= 3:
            # Это JSON события от droid - извлекаем человеческий текст
            human_parts = []
            
            for line in lines:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    event_type = event.get('type')
                    
                    # Собираем человеческий текст из событий
                    if event_type == 'message' and event.get('role') == 'assistant':
                        msg_text = event.get('text', '')
                        if msg_text:
                            human_parts.append(msg_text)
                    
                    elif event_type == 'completion':
                        final_text = event.get('finalText', '')
                        if final_text:
                            human_parts.append(final_text)
                            
                except json.JSONDecodeError:
                    # Не JSON строка - возможно обычный текст между событиями
                    if line.strip() and not line.startswith('{'):
                        human_parts.append(line)
            
            return '\n'.join(human_parts).strip()
        
        else:
            # Обычный текст (copilot/codex) - отрезаем JSON логи в конце
            if '{"type":' in text:
                # Ищем с конца, чтобы не задеть JSON в коде
                idx = text.rfind('{"type":')
                # Если это похоже на лог (близко к концу)
                if idx > len(text) - 500: 
                    text = text[:idx]
            
            return text.strip()
    
    async def _extract_findings_with_llm(self, review_output: str, iteration: int = 0) -> List[Finding]:
        """Извлечь findings из вывода reviewer через LLM (надёжнее чем regex).

        Передаём вывод reviewer в LLM, который решает есть ли ошибки и извлекает их
        в структурированном виде. Надёжнее regex потому что LLM понимает контекст.
        При ошибке LLM — fallback на _parse_findings() (regex).
        """
        # Убираем ANSI escape codes (используем существующий метод)
        clean_output = self._strip_ansi(review_output)

        # Ограничиваем длину чтобы не превысить контекст LLM (~4k токенов)
        max_output_len = 15000
        if len(clean_output) > max_output_len:
            clean_output = "...(truncated)...\n" + clean_output[-max_output_len:]

        logger.info("[ReviewLoop] Extracting findings with LLM from %d chars", len(clean_output))

        prompt = EXTRACT_FINDINGS_PROMPT.format(review_output=clean_output)

        try:
            result = await self.llm.generate_json(prompt, temperature=0.1)

            has_issues = result.get("has_issues", False)
            summary = result.get("summary", "")
            raw_findings = result.get("findings", [])

            logger.info("[ReviewLoop] LLM extraction: has_issues=%s, summary=%s", has_issues, summary)

            if not has_issues:
                logger.info("[ReviewLoop] LLM says no issues found")
                return []

            findings = []
            for f in raw_findings:
                severity = f.get("severity", "MEDIUM").upper()
                if severity not in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
                    severity = "MEDIUM"

                description = f.get("description", "")
                if not description:
                    continue

                location = f.get("location")
                if location == "null" or location == "":
                    location = None

                findings.append(Finding(
                    severity=severity,
                    description=description,
                    location=location,
                ))
                logger.info("[ReviewLoop] LLM finding: %s - %s...", severity, description[:60])

            logger.info("[ReviewLoop] LLM extracted %d findings", len(findings))
            try:
                self._findings_store.save(findings, clean_output, iteration)
            except Exception as e:
                logger.warning(f"[ReviewLoop] Failed to save findings to store: {e}")
            return findings

        except Exception as e:
            logger.warning("[ReviewLoop] LLM extraction failed: %s, falling back to regex", e)
            return self._parse_findings(review_output)

    def _parse_findings(self, codex_output: str, iteration: int = 0) -> List[Finding]:
        """
        Парсинг с защитой от дурака.
        Если regex не сработал -> возвращаем весь текст как одну ошибку.
        """
        findings = []
        
        # 1. Получаем текст (теперь он не будет пустым благодаря новому методу выше)
        actual_response = self._extract_human_response(codex_output)
        
        # Логируем, что мы реально получили
        logger.info(f"[ReviewLoop] Raw extracted length: {len(actual_response)}")
        
        # 2. Если текст пустой (вообще беда)
        if not actual_response:
            logger.error("[ReviewLoop] EMPTY RESPONSE extracted!")
            return []

        # 3. Пробуем найти красивые ошибки через Regex (твой старый список + фиксы)
        patterns = [
            r'^(CRITICAL|HIGH|MEDIUM|LOW):\s*(.+?)(?:\s*\[([^\]]+)\])?$',
            r'^\d+\.\s*\*\*?(CRITICAL|HIGH|MEDIUM|LOW)\*\*?\s*[-–:]\s*(.+)$',
            r'^[•\-\*]\s*(CRITICAL|HIGH|MEDIUM|LOW)[:\-–]\s*(.+)$',
            r'^\[(CRITICAL|HIGH|MEDIUM|LOW)\]\s*(.+)$',
            r'(CRITICAL|HIGH|MEDIUM|LOW)\s*[:\-–]\s*(.+)$'  # Самый жадный паттерн
        ]

        seen = set()
        for line in actual_response.split('\n'):
            line = line.strip()
            if not line: continue
            if "Проведи ДОТОШНУЮ" in line: continue  # Пропускаем заголовок промпта если он остался
            # Пропускаем строки из инструкций ревью (severity definitions из промпта)
            if any(skip in line for skip in [
                "Умеренные проблемы, можно работать",
                "Критические баги, крэши, security",
                "Серьёзные проблемы, неправильная логика",
                "Мелкие замечания, стиль",
                "БУДУТ ПРОИГНОРИРОВАНЫ",
                "ОБЯЗАТЕЛЬНО исправить",
                "описание проблемы. файл:строка",
                "ПРАВИЛА SEVERITY",
                "Previous iterations (avoid",
                "Parser failed to split",
            ]): continue

            for pattern in patterns:
                match = re.match(pattern, line, re.IGNORECASE)
                if match:
                    groups = match.groups()
                    sev = groups[0].upper()
                    desc = groups[1].strip().strip('*').strip('.')
                    loc = groups[2] if len(groups) > 2 else None
                    
                    if desc not in seen:
                        seen.add(desc)
                        findings.append(Finding(sev, desc, loc))
                    break

        # =================================================================
        # 4. 🔥 ГЛАВНОЕ ИСПРАВЛЕНИЕ: FALLBACK 🔥
        # Если Regex ничего не нашел, но текст длинный -> это и есть ошибка!
        # =================================================================
        if not findings and len(actual_response) > 50:
            # Проверяем: ревьюер сказал "проблем не найдено"?
            no_problems_markers = [
                "проблем не найдено",
                "проблемы не найдены",
                "no problems found",
                "no issues found",
                "всё хорошо",
                "все хорошо",
                "код качественный",
            ]
            response_lower = actual_response.lower()
            if any(marker in response_lower for marker in no_problems_markers):
                logger.info("[ReviewLoop] Reviewer said NO PROBLEMS — treating as clean review.")
                # Не добавляем findings — loop остановится
            else:
                logger.warning("[ReviewLoop] Regex found nothing. Using RAW OUTPUT as finding.")
                
                # Создаем "искусственную" ошибку, содержащую весь текст
                # Обрезаем если слишком огромная, чтобы не ломать JSON
                content = actual_response
                if len(content) > 10000:
                    content = content[:10000] + "...(truncated)"
                    
                findings.append(Finding(
                    severity="HIGH",
                    description=f"Raw Review Output (Parser failed to split): \n{content}",
                    location="See logs"
                ))

        # 5. Сохраняем (используем твой исправленный FindingsStore)
        try:
            # Важно: передаем actual_response как raw_output
            self._findings_store.save(findings, actual_response, iteration)
        except Exception as e:
            logger.error(f"[ReviewLoop] Save failed: {e}")

        return findings

    def _seed_initial_errors(self, initial_errors: List[str]) -> int:
        """Parse and store initial errors for continue mode.

        Returns number of parsed findings.
        """


        findings: List[Finding] = []
        for line in initial_errors:
            if not line:
                continue
            text = line.strip().lstrip("-").strip()
            if not text:
                continue
            m = re.match(r"^(CRITICAL|HIGH|MEDIUM|LOW)\s*[:\-]\s*(.+)$", text, re.IGNORECASE)
            if not m:
                continue
            severity = m.group(1).upper()
            rest = m.group(2).strip()
            location = None

            # Optional location at the end: [file:line] or (file:line)
            mloc = re.search(r"\[([^\]]+)\]\s*$", rest)
            if mloc:
                location = mloc.group(1).strip()
                rest = rest[:mloc.start()].strip()
            else:
                mloc = re.search(r"\(([^)]+)\)\s*$", rest)
                if mloc:
                    location = mloc.group(1).strip()
                    rest = rest[:mloc.start()].strip()

            if not rest:
                continue

            findings.append(Finding(
                severity=severity,
                description=rest,
                location=location,
            ))

        if findings:
            raw_output = "\n".join(initial_errors)
            try:
                self._findings_store.save(findings, raw_output, 0)
            except Exception as e:
                logger.warning(f"[ReviewLoop] Failed to seed initial errors: {e}")
        return len(findings)
    
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
    
    def _prepare_fix_task_raw(self, original_task: str, review_output: str) -> str:
        """ПРОСТАЯ И НАДЁЖНАЯ передача: весь ЧИСТЫЙ вывод reviewer"""

        # ИСПОЛЬЗУЕМ НОВЫЙ МЕТОД ОЧИСТКИ
        # Это гарантирует, что Copilot увидит только описание ошибок, а не JSON-логи
        clean_output = self._extract_human_response(review_output)
        
        # Ограничиваем длину (на всякий случай, хотя без JSON текст будет короче)
        max_len = 25000 
        if len(clean_output) > max_len:
            clean_output = f"...(начало обрезано)...\n\n" + clean_output[-max_len:]
            
        fix_task = f"""ИСПРАВЬ ПРОБЛЕМЫ НАЙДЕННЫЕ РЕВЬЮЕРОМ:

=== ОРИГИНАЛЬНАЯ ЗАДАЧА ===
{original_task}

=== ОТЧЕТ ОБ ОШИБКАХ ===
{clean_output}
=== КОНЕЦ ОТЧЕТА ===

⚠️ ВАЖНЫЕ ПРАВИЛА ⚠️
1. Исправь ТОЛЬКО проблемы уровня CRITICAL, HIGH и MEDIUM
2. Проблемы уровня LOW - ИГНОРИРУЙ.
3. Если список ошибок пуст или там написано "Проблем не найдено" - ничего не меняй.
"""
        
        # ДИАГНОСТИКА: Сохраняем fix_task в файл для отладки
        try:
            debug_file = Path(tempfile.gettempdir()) / "bender-last-fix-task.txt"
            debug_file.write_text(fix_task)
            logger.info(f"[ReviewLoop] _prepare_fix_task_raw: saved to {debug_file} ({len(fix_task)} chars)")
        except Exception as e:
            logger.warning(f"[ReviewLoop] Failed to save debug file: {e}")
        
        return fix_task

    @staticmethod
    def _strip_ansi(text: str) -> str:

        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        return ansi_escape.sub('', text or '')

    def _review_output_is_empty(self, output: str) -> bool:
        """Return True if review output looks like prompt-only/no findings."""
        if not output or not output.strip():
            return True
        clean = self._strip_ansi(output)
        # Valid markers
        if ("Проблем не найдено" in clean or "No issues found" in clean):
            return False

        if re.search(r'\b(CRITICAL|HIGH|MEDIUM|LOW)\b\s*:', clean, re.IGNORECASE):
            return False
        # If it mostly looks like the prompt header, treat as empty
        prompt_markers = [
            "Проведи ДОТОШНУЮ проверку кода",
            "Критерии приёмки",
            "Проверь:",
        ]
        if any(m in clean for m in prompt_markers):
            return True
        return False
    
    def _prepare_fix_task(
        self,
        original_task: str,
        findings: List[Finding],
        fix_instructions: Optional[str],
    ) -> str:
        """Подготовить задачу для следующей итерации Copilot"""
        
        logger.info(f"[ReviewLoop] _prepare_fix_task: received {len(findings)} findings")
        for i, f in enumerate(findings):
            logger.info(f"[ReviewLoop]   Finding {i+1}: {f.severity} - {f.description[:80]}...")
        
        findings_text = "\n".join([
            f"- {f.severity}: {f.description}" + (f" ({f.location})" if f.location else "")
            for f in findings
            if f.severity in ("CRITICAL", "HIGH", "MEDIUM")  # LOW пропускаем
        ])
        
        logger.info(f"[ReviewLoop] _prepare_fix_task: findings_text length = {len(findings_text)}")
        if findings_text:
            logger.info(f"[ReviewLoop] _prepare_fix_task: findings_text preview:\n{findings_text[:500]}")
        else:
            logger.warning("[ReviewLoop] _prepare_fix_task: findings_text is EMPTY!")
        
        task = f"""ИСПРАВЬ НАЙДЕННЫЕ ПРОБЛЕМЫ:

Оригинальная задача: {original_task}

Code review нашёл следующие проблемы:
{findings_text}

{f"Инструкции: {fix_instructions}" if fix_instructions else ""}

Исправь эти проблемы. После исправления код снова будет проверен."""
        
        logger.info(f"[ReviewLoop] _prepare_fix_task: final task length = {len(task)}")
        logger.info(f"[ReviewLoop] _prepare_fix_task: final task preview:\n{task[:800]}")
        
        return task
    
    def _prepare_fix_task_from_store(self, original_task: str) -> str:
        """Подготовить fix task используя FindingsStore (НАДЁЖНЫЙ СПОСОБ)
        
        Читает findings из JSON файла, что гарантирует их сохранность между итерациями.
        """
        findings_text, is_valid = self._findings_store.get_for_fix_task()
        
        if not findings_text:
            logger.error("[ReviewLoop] _prepare_fix_task_from_store: NO FINDINGS!")
            # Возвращаем generic task
            return f"""ПРОВЕРЬ КОД НА ПРОБЛЕМЫ:

Оригинальная задача: {original_task}

Reviewer нашёл проблемы но данные были потеряны.
Перепроверь код и исправь найденные проблемы."""
        
        if is_valid:
            # Данные из структурированных findings
            task = f"""ИСПРАВЬ ВСЕ ПРОБЛЕМЫ НИЖЕ:

Оригинальная задача: {original_task}

=== FINDINGS ===
{findings_text}
=== КОНЕЦ FINDINGS ===

ИНСТРУКЦИИ:
1. Исправь ВСЕ проблемы начиная с CRITICAL
2. Для каждой проблемы указан severity и описание
3. После исправления проверь что код работает"""
            logger.info(f"[ReviewLoop] _prepare_fix_task_from_store: generated task with structured findings ({len(findings_text)} chars)")
        else:
            # Fallback: raw output
            task = f"""ИСПРАВЬ ПРОБЛЕМЫ НАЙДЕННЫЕ РЕВЬЮЕРОМ:

Оригинальная задача: {original_task}

=== ВЫВОД REVIEW ===
{findings_text[:8000]}
=== КОНЕЦ ===

Найди проблемы в выводе и исправь их."""
            logger.warning(f"[ReviewLoop] _prepare_fix_task_from_store: using raw output fallback ({len(findings_text)} chars)")
        
        return task
    
    async def _should_continue(self, review_output: str) -> tuple[bool, str]:
        """Анализирует, нужно ли продолжать цикл исправления"""


        
        # 1. Получаем ЧИСТЫЙ текст ответа (без промпта и без JSON)
        clean_text = self._extract_human_response(review_output)
        
        logger.info(f"[ReviewLoop] Analyzing clean response ({len(clean_text)} chars)")

        # 2. Сначала ищем CRITICAL/HIGH (Самый высокий приоритет)
        # Если они есть - плевать на всё остальное, надо чинить.
        has_critical = bool(re.search(r'\b(CRITICAL|HIGH)\b', clean_text, re.IGNORECASE))
        if has_critical:
            return True, "CRITICAL/HIGH issues detected"

        # 3. Проверяем явный маркер "Проблем не найдено"
        # Теперь мы ищем его только в чистом тексте, ложные срабатывания из промпта исключены
        if "проблем не найдено" in clean_text.lower() or "no issues found" in clean_text.lower():
            return False, "No issues found (marker detected)"
            
        # 4. Проверяем MEDIUM
        has_medium = bool(re.search(r'\bMEDIUM\b', clean_text, re.IGNORECASE))
        if has_medium:
            return True, "MEDIUM issues detected"
            
        # 5. Если остались только LOW или вообще ничего не нашли
        # (обычно если ничего не нашли и нет маркера "no issues" - это странно, но лучше остановить, чем бесконечно крутить)
        has_low = bool(re.search(r'\bLOW\b', clean_text, re.IGNORECASE))
        if has_low:
            return False, "Only LOW issues found (ignored)"
            
        # Fallback: если текст слишком короткий — это СБОЙ reviewer, надо RETRY
        min_chars = 500 if self.use_party_mode else 200
        if len(clean_text) < min_chars:
            logger.warning(f"[ReviewLoop] Review output too short ({len(clean_text)} < {min_chars} chars) — treating as failed review, will retry")
            return True, f"Review output too short ({len(clean_text)} chars) — retrying"

        # Party mode: отсутствие маркеров = сбой review (Party ОБЯЗАН выдать scores)
        if self.use_party_mode:
            logger.warning(f"[ReviewLoop] Party mode: no severity markers in {len(clean_text)} chars — retrying")
            return True, "Party mode: no severity markers found — retrying review"
        
        # Normal mode: маркеров нет, "no issues" нет — скорее всего болтовня, останавливаем
        return False, "No severity markers found"
    
    def _prepare_fix_task_simple(self, original_task: str, review_output: str) -> str:
        """Извлечь findings из РЕАЛЬНОГО ответа (после отсечения промпта).
        
        Структура terminal output:
        ━━━ (1) + 🤖 BENDER → + ━━━ (2) + ПРОМПТ + ━━━ (3) + РЕАЛЬНЫЙ ОТВЕТ
        
        Мы берём ТОЛЬКО parts[3:] и парсим findings оттуда.
        """

        
        # Убираем ANSI escape codes
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        clean_output = ansi_escape.sub('', review_output)
        
        logger.info(f"[ReviewLoop] _prepare_fix_task_simple: input {len(clean_output)} chars")
        
        # ШАГ 1: Отрезаем промпт - берём ТОЛЬКО реальный ответ после 3-го разделителя
        separator = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        real_response = clean_output
        
        if separator in clean_output:
            parts = clean_output.split(separator)
            logger.info(f"[ReviewLoop] Split into {len(parts)} parts")
            if len(parts) >= 4:
                # parts[3:] = реальный ответ (всё после 3-го разделителя)
                real_response = separator.join(parts[3:])
                logger.info(f"[ReviewLoop] Extracted REAL response: {len(real_response)} chars (removed prompt)")
            elif len(parts) >= 3:
                real_response = separator.join(parts[2:])
                logger.info(f"[ReviewLoop] Fallback: took parts[2:]: {len(real_response)} chars")
        
        # ШАГ 2: Парсим findings ТОЛЬКО из реального ответа
        pattern = re.compile(r'^[\s\-\*]*\*?\*?(CRITICAL|HIGH|MEDIUM|LOW)[:\s]+(.+)$', re.MULTILINE | re.IGNORECASE)
        
        findings = []
        for match in pattern.finditer(real_response):
            severity = match.group(1).upper()
            desc = match.group(2).strip().rstrip('*')
            # Фильтруем placeholder'ы из промпта (на случай если не отрезали)
            if 'описание проблемы' in desc.lower() or 'файл:строка' in desc.lower():
                continue
            if 'CRITICAL/HIGH/MEDIUM/LOW' in desc:
                continue
            findings.append(f"- {severity}: {desc}")
        
        # Дедупликация
        seen = set()
        unique_findings = []
        for f in findings:
            f_norm = f.lower().strip()
            if f_norm not in seen:
                seen.add(f_norm)
                unique_findings.append(f)
        
        logger.info(f"[ReviewLoop] Extracted {len(unique_findings)} unique findings from real response")
        
        if not unique_findings:
            logger.warning("[ReviewLoop] No findings extracted! Using fallback")
            # Fallback: берём первые 8000 символов реального ответа
            truncated = real_response[:8000] if len(real_response) > 8000 else real_response
            return f"""ИСПРАВЬ ПРОБЛЕМЫ НАЙДЕННЫЕ РЕВЬЮЕРОМ:

Оригинальная задача: {original_task}

=== ВЫВОД REVIEW ===
{truncated}
=== КОНЕЦ ===

Найди проблемы в выводе и исправь их."""
        
        # Формируем чистый список findings
        findings_text = "\n".join(unique_findings)
        
        task = f"""ИСПРАВЬ ВСЕ ПРОБЛЕМЫ НИЖЕ:

Оригинальная задача: {original_task}

=== FINDINGS ({len(unique_findings)} проблем) ===
{findings_text}
=== КОНЕЦ FINDINGS ===

ИНСТРУКЦИИ:
1. Исправь ВСЕ проблемы начиная с CRITICAL
2. Для каждой проблемы указан файл:строка - иди туда и исправляй
3. После исправления проверь что код работает"""
        
        logger.info(f"[ReviewLoop] Generated fix task with {len(unique_findings)} findings")
        return task
