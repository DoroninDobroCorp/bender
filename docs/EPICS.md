# Parser Maker - Epics & Stories

## Обзор Epics

| Epic | Название | Приоритет | Effort | Статус |
|------|----------|-----------|--------|--------|
| 1 | Core Infrastructure | CRITICAL | 10-16h | PENDING |
| 2 | Bender (Gemini + GLM) | CRITICAL | 28-40h | PENDING |
| 3 | 6-Step Pipeline | CRITICAL | 16-24h | PENDING |
| 4 | State & Recovery | HIGH | 12-16h | PENDING |
| 5 | User Interface | MEDIUM | 12-18h | PENDING |

**Total: 78-114h (10-15 dev-days)**

---

# Epic 1: Core Infrastructure

**Цель:** Базовая инфраструктура для управления Droid.

## Story 1.1: Droid Controller (M: 8-12h)

**Problem:** Нужен надёжный способ управления Droid через tmux.

**Solution:** Контроллер на базе VibeCoder с упрощениями (без переключения моделей).

**Acceptance Criteria:**
- [ ] Запуск Droid в tmux сессии (только дорогая модель)
- [ ] Отправка команд с ожиданием ответа
- [ ] Детекция approval requests и автоматический approve
- [ ] Детекция завершения работы (idle detection)
- [ ] Открытие нового чата (/new)
- [ ] Graceful shutdown
- [ ] Логирование всех взаимодействий

**Files:** `core/droid_controller.py`

---

## Story 1.2: Configuration System (S: 2-4h)

**Problem:** Нужна гибкая конфигурация.

**Solution:** Pydantic Settings с .env поддержкой.

**Acceptance Criteria:**
- [ ] Загрузка из .env файла
- [ ] Валидация обязательных полей
- [ ] Настройки Bender (Gemini API key)
- [ ] Настройки Git (auto commit/push)
- [ ] Настройки отображения (visible/silent)

**Files:** `core/config.py`, `.env.example`

---

# Epic 2: Bender (Gemini Supervisor)

**Цель:** Джун-помощник на Gemini который следит за Droid.

## Story 2.1: Watchdog (M: 6-8h)

**Problem:** Droid может зависнуть, зациклиться или вылететь.

**Solution:** Watchdog который мониторит здоровье Droid.

**Acceptance Criteria:**
- [ ] Детекция зависания (проверка логов каждые 5 мин, таймаут 1 час = 12 проверок)
- [ ] Bender читает логи и решает: ждать/пинать/перезапуск
- [ ] Детекция зацикливания (одинаковые сообщения 3+ раз)
- [ ] Детекция вылета (процесс tmux умер)
- [ ] Детекция ошибок (Exception/Error в логах)
- [ ] Действия: пинок Enter, перезапуск, новый чат

**Files:** `bender/watchdog.py`

---

## Story 2.2: Response Analyzer (M: 8-10h)

**Problem:** Нужно понимать ответы Droid - сделал изменения или нет.

**Solution:** Gemini анализирует ответ Droid.

**Acceptance Criteria:**
- [ ] Парсинг ответа Droid
- [ ] Определение: были ли изменения?
- [ ] Определение: существенные или косметические?
- [ ] Определение: выполнено ли ТЗ шага?
- [ ] JSON output для системы

**Files:** `bender/analyzer.py`

---

## Story 2.3: Task Enforcer (M: 6-8h)

**Problem:** Если Droid не закончил ТЗ - нужно настоять.

**Solution:** Enforcer который пинает Droid закончить работу.

**Acceptance Criteria:**
- [ ] Проверка выполнения ТЗ по ответу Droid
- [ ] Генерация сообщений "заверши задачу"
- [ ] Требование показать результат
- [ ] Лимит попыток настаивания (после N неудач - эскалация к человеку)

**Files:** `bender/enforcer.py`

---

## Story 2.4: Gemini Client + GLM Fallback (M: 6-8h)

**Problem:** Нужен клиент для Gemini API с fallback при недоступности.

**Solution:** Async клиент с автоматическим переключением на GLM.

**Acceptance Criteria:**
- [ ] Поддержка моделей Gemini: gemini-2.5-pro, gemini-3-pro, gemini-3-flash
  # ВАЖНО: Названия моделей НЕ МЕНЯТЬ - проверены и существуют на январь 2025
- [ ] **Fallback на GLM-4.6+** при:
  - Gemini API недоступен (5xx, timeout)
  - Rate limit exceeded
  - Quota exceeded
- [ ] **Llama модели ЗАПРЕЩЕНЫ** - не использовать ни при каких условиях
- [ ] Автоматический retry с exponential backoff (3 попытки Gemini, потом GLM)
- [ ] JSON mode для структурированных ответов
- [ ] Логирование какой провайдер использовался
- [ ] Конфиг: можно отключить fallback или сделать GLM primary

**Files:** `bender/gemini_client.py`, `bender/glm_client.py`, `bender/llm_router.py`

---

## Story 2.5: Bender Supervisor Main (M: 6-8h)

**Problem:** Нужен координатор всех компонентов Bender.

**Solution:** Главный класс BenderSupervisor.

**Acceptance Criteria:**
- [ ] Координация watchdog, analyzer, enforcer
- [ ] Gemini API клиент
- [ ] Human escalation (в крайнем случае)
- [ ] Логирование всех решений
- [ ] Режим visible/silent для вывода мыслей

**Files:** `bender/supervisor.py`

---

# Epic 3: 6-Step Pipeline

**Цель:** Реализация 6-шагового процесса с git и 2x confirmation.

## Story 3.1: Step Definition (S: 4-6h)

**Problem:** Нужна структура для описания 6 шагов.

**Solution:** YAML конфиг с промптами для каждого шага.

**Acceptance Criteria:**
- [ ] Структура Step: id, name, prompt_template
- [ ] Загрузка из YAML
- [ ] Подстановка переменных в промпт
- [ ] 6 готовых шагов для парсера

**Files:** `pipeline/step.py`, `steps/parser_steps.yaml`

---

## Story 3.2: Git Manager (S: 4-6h)

**Problem:** После каждой итерации с изменениями нужен git commit/push.

**Solution:** Git Manager для автоматических коммитов.

**Acceptance Criteria:**
- [ ] `git add .`
- [ ] `git commit -m "Step N, iteration M: <summary>"`
- [ ] `git push`
- [ ] Обработка ошибок (конфликты, нет remote) - эскалация к человеку
- [ ] Опция отключить auto-push
- [ ] Git commit только при открытии нового чата (после существенных изменений)

**Files:** `pipeline/git_manager.py`

---

## Story 3.3: Pipeline Orchestrator (L: 10-14h)

**Problem:** Нужен координатор 6 шагов с логикой 2x confirmation.

**Solution:** Orchestrator который управляет flow.

**Acceptance Criteria:**
- [ ] Загрузка 6 шагов
- [ ] Цикл: промпт → Droid → Bender анализ → решение
- [ ] Логика 2x "нет изменений" = следующий шаг
- [ ] Git commit после существенных изменений
- [ ] Новый чат после каждой итерации
- [ ] Progress events для UI

**Files:** `pipeline/orchestrator.py`

---

# Epic 4: State & Recovery

**Цель:** Сохранение состояния и возможность продолжить.

## Story 4.1: State Persistence (M: 6-8h)

**Problem:** При сбое теряется прогресс.

**Solution:** Сохранение состояния в JSON.

**Acceptance Criteria:**
- [ ] Сохранение после каждой итерации
- [ ] Структура: step, iteration, confirmations, git_commits
- [ ] Atomic writes
- [ ] Автоматический backup

**Files:** `state/persistence.py`

---

## Story 4.2: Resume & Recovery (M: 6-8h)

**Problem:** Нужно продолжить с места остановки, включая mid-iteration сбои.

**Solution:** Resume command с поддержкой recovery из stash.

**Acceptance Criteria:**
- [ ] Загрузка последнего состояния
- [ ] Продолжение с текущего шага
- [ ] **Mid-iteration recovery:**
  - [ ] При старте проверка `git status` на uncommitted changes
  - [ ] Автоматический stash: `git stash push -m "recovery_step_N_iter_M"`
  - [ ] При resume: `git stash pop` если есть recovery stash
  - [ ] Новый чат с просьбой проверить/доделать
- [ ] Опция начать шаг заново (отбросить stash)
- [ ] Список всех runs

**Files:** `state/recovery.py`

---

# Epic 5: User Interface

**Цель:** Удобный интерфейс с режимами visible/silent.

## Story 5.1: CLI Interface (M: 6-8h)

**Problem:** Нужен CLI для запуска.

**Solution:** Click-based CLI.

**Acceptance Criteria:**
- [ ] `parser-maker run <project>` - запуск
- [ ] `parser-maker resume` - продолжить
- [ ] `parser-maker status` - статус
- [ ] `--visible` / `--silent` флаги
- [ ] Colored output

**Files:** `cli/main.py`

---

## Story 5.2: Display Modes (M: 6-8h)

**Problem:** Нужны режимы отображения.

**Solution:** Visible и Silent режимы.

**Acceptance Criteria:**
- [ ] **Visible**: мысли Bender, output Droid, git операции
- [ ] **Silent**: только прогресс и результат
- [ ] Live update в терминале
- [ ] Логирование в файл (всегда полное)

**Files:** `cli/display.py`

---

## Story 5.3: Telegram Notifications (S: 4-6h)

**Problem:** Хочется уведомления о прогрессе.

**Solution:** Опциональный Telegram bot с fallback на desktop notifications.

**Acceptance Criteria:**
- [ ] Уведомление о завершении шага
- [ ] Уведомление об escalation
- [ ] Финальный отчёт
- [ ] Команда /abort
- [ ] **Fallback**: desktop notification (macOS) или звук если Telegram не настроен

**Files:** `integrations/telegram.py`, `integrations/notifications.py`

---

# Implementation Roadmap

## Phase 1: Core + Bender (Week 1)
- Epic 1: Core Infrastructure
- Epic 2: Bender Supervisor

**Deliverable:** Работающий Bender который следит за Droid

## Phase 2: Pipeline (Week 2)
- Epic 3: 6-Step Pipeline

**Deliverable:** Полный цикл с git и 2x confirmation

## Phase 3: Reliability + UX (Week 3)
- Epic 4: State & Recovery
- Epic 5: User Interface

**Deliverable:** Готовый продукт
