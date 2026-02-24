"""Промпты для review loop v2 — без парсинга findings."""

# ──────────────────────────────────────────────────────
# REVIEW PROMPT — reviewer проверяет код
# ──────────────────────────────────────────────────────
REVIEW_PROMPT = """Проведи ДОТОШНУЮ проверку кода:

Контекст задачи: {context}

Критерии приёмки:
{criteria}

Проверь:
1. Код на ошибки, баги, уязвимости
2. Соответствие КАЖДОМУ критерию приёмки выше
3. Запусти проект если нужно
4. Проверь что всё работает

ВАЖНО:
- Будь дотошным, но НЕ придумывай ошибки ради галочки
- НЕ пиши про мелкий code style / форматирование / "можно улучшить"
- Только РЕАЛЬНЫЕ проблемы которые нужно исправить
- Если нет проблем — напиши "Проблем не найдено"

Формат не важен — пиши свободно, главное описать реальные проблемы."""


# ──────────────────────────────────────────────────────
# REVIEW PROMPT PARTY — BMAD Party 10-ролевая оценка
# ──────────────────────────────────────────────────────
REVIEW_PROMPT_PARTY = """Проведи BMAD Party Review кода — оценку от 10 ролей:

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

⚠️ НЕ завышай оценки! Реальные тесты, реальный coverage, реальные проверки.
⚠️ Если pytest/mypy/ruff показали ошибки — это МИНИМУМ HIGH."""


# ──────────────────────────────────────────────────────
# FIX PROMPT — executor получает ВЕСЬ review и чинит
# ──────────────────────────────────────────────────────
FIX_PROMPT = """Вот результат code review для задачи: "{task}"

=== REVIEW ===
{review_output}
=== КОНЕЦ REVIEW ===

Прочитай review и исправь РЕАЛЬНЫЕ проблемы.

ПРАВИЛА:
1. Исправляй баги, крэши, security-дыры, сломанную логику
2. НЕ исправляй: стиль, форматирование, "можно улучшить", опечатки в комментариях
3. Если review говорит "всё хорошо" или "проблем не найдено" — НЕ МЕНЯЙ НИЧЕГО

В КОНЦЕ обязательно напиши ОДНУ из строк:
STATUS: CHANGED
STATUS: NO_CHANGES

Если STATUS: NO_CHANGES — обязательно укажи:
1. Какой BMAD score пришёл в review (если был BMAD Party — процитируй Average и Min)
2. Подробно объясни почему ничего не менял (нет реальных багов, замечания косметические, и т.д.)
3. Если score < 98 — объясни конкретно какие пункты ты не можешь исправить и почему"""
