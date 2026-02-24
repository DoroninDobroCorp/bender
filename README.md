🇬🇧 [English](#-english) | 🇷🇺 [Русский](#-русский)

---

# 🇬🇧 English

# Bender - AI Task Supervisor

Bender is a supervisor for AI CLI tools (GitHub Copilot, Droid, Codex). It doesn't solve tasks itself — it makes sure senior models execute them correctly.

## Concept

```
You give a task → Bender analyzes complexity → Picks a worker →
→ Monitors execution → Nudges if stuck → Verifies the result
```

## Installation

```bash
# 1. Clone
git clone https://github.com/DoroninDobroCorp/bender.git
cd bender

# 2. Install as CLI
pip install -e .

# 3. Configure .env
cp .env.example .env
# Fill in GLM_API_KEY (Cerebras)

# 4. Verify
bender --help
```

## Requirements

- Python 3.10+
- macOS with Terminal.app (for interactive mode)
- tmux (for visible mode without -I)
- GitHub Copilot CLI (`copilot`)
- Optional: `droid`, `codex`

## Quick Start

```bash
# Simple run — bender picks the worker automatically
bender run "Add a test for the auth module"

# Visible mode — opens a terminal with copilot
bender run -v "Add a test for the auth module"

# Interactive mode — full terminal, you can take over manually
bender run -vI "Add a test for the auth module"

# Review loop — iterative cycle until all issues are resolved
bender run -lvI "Add OAuth authorization"
```

## Modes of Operation

### 1. Standard Mode (`bender run`)

```bash
bender run "task"             # Auto-select worker
bender run --droid "task"     # Force droid (simple tasks)
bender run --opus "task"      # Force copilot (medium tasks)
bender run --codex "task"     # Force codex (complex tasks)
```

### 2. Visible Mode (`-v`)

Opens a terminal window with a tmux session:

```bash
bender run -v "task"
```

- See what copilot is doing in real time
- Scroll through history (scrollback)
- Bender closes the window when done

### 3. Interactive Mode (`-I`) ⭐ NEW

**Native terminal** — exactly the same as working with copilot yourself:

```bash
bender run -vI --simple "task"
```

**How it works:**
- Bender opens a **new Terminal.app window** (not tmux!)
- Launches copilot with your task
- The terminal stays open while running — you can scroll freely
- Once finished, the terminal **closes automatically**
- Bender automatically responds to permission prompts (y/n)

**Advantages:**
- Terminal is EXACTLY the same as when you work manually
- Full scrollback — scroll as much as you want
- You can intervene at any moment (while it's running)
- Without `--simple` — analyzes the task first and adds acceptance criteria

### 4. Review Loop (`-l`)

Iterative cycle: copilot executes → reviewer checks → copilot fixes:

```bash
bender run -l "task"          # Copilot → Codex → Copilot
bender run -lc "task"         # Copilot → Copilot (saves codex quota)
bender run -lvI "task"        # With interactive terminal
```

**How it works:**
1. **Copilot** executes the task
2. **Reviewer** thoroughly checks (BMAD roles, visual inspection, tests)
3. **GLM** analyzes findings:
   - `fix` — needs to be fixed
   - `skip` — can be skipped
   - `done` — everything is good
4. If `fix` → new Copilot session with instructions
5. Repeat until `done` or max iterations reached

## All Options

```
bender run [OPTIONS] [TASK]

Options:
  --droid               Force droid (simple tasks)
  --opus                Force copilot (medium tasks)
  --codex               Force codex (complex tasks)
  -a, --auto            Auto-select by complexity (default)
  -i, --interval N      Log check interval in seconds (default: 60)
  -s, --simple          Skip analysis and verification
  -v, --visible         Show terminals
  -I, --interactive     Interactive mode (full terminal)
  -l, --review-loop     Iterative copilot→reviewer cycle
  -c, --copilot-review  Use copilot instead of codex for review
  -d, --droid-mode      Use droid for execution and review
  --max-iterations N    Max iterations for review loop (default: 10)
  -C, --continue-errors Continue with known errors (comma-separated)
  -E, --errors-interactive  Enter errors interactively
  -p, --project PATH    Path to project
  --help                Help
```

## Usage Examples

```bash
# Simple task
bender run "Fix the typo in README"

# Task with visible terminal
bender run -v "Add a unit test for auth"

# Interactive mode — you can continue if bender crashes
bender run -vI "Refactor the payments module"

# Review loop with copilot reviewer (saves codex quota)
bender run -lvIc "Add OAuth authorization"

# Continue with known errors
bender run -lvIc -C "MEDIUM: missing email validation" "Finish the form"

# Fully automatic mode (no visible terminal)
bender run -l "Add an API endpoint for users"
```

## Other Commands

```bash
bender status    # Current task status
bender attach    # Attach to tmux session
```

## Configuration (.env)

```env
# Required
GLM_API_KEY=csk-...           # Cerebras API key

# Optional (multiple keys, comma-separated)
GLM_API_KEYS=csk-key1,csk-key2,csk-key3

# Optional
DROID_PROJECT_PATH=/path/to/project
AUTO_GIT_PUSH=true
```

## LLM

- **Model:** Qwen (`qwen-3-235b-a22b-instruct-2507`) via Cerebras
- Rate limit: 30 req/min on the free tier
- Auto-retry with exponential backoff across keys
- Supports rotation of multiple API keys

**On 429 rate limit:** Use `-s` (simple mode) — works WITHOUT GLM analysis:
```bash
bender run -vIs "task"       # Interactive without GLM
bender run -lvIs "task"      # Review loop without GLM analysis
```

## Project Structure

```
bender/
├── bender/              # Core source code
│   ├── glm_client.py    # GLM API client
│   ├── llm_router.py    # GLM + Qwen fallback
│   ├── review_loop.py   # Iterative review cycle
│   ├── task_clarifier.py
│   ├── task_manager.py
│   ├── worker_manager.py
│   ├── log_watcher.py
│   └── workers/
│       ├── base.py              # Base worker
│       ├── copilot.py           # Copilot (non-interactive)
│       ├── interactive_copilot.py  # Copilot (interactive) ⭐
│       ├── droid.py
│       └── codex.py
├── bender_cli/          # CLI interface
│   └── main.py
├── core/                # Configuration
│   ├── config.py
│   └── logging_config.py
└── tests/
```

## Logs

Logs are saved to `logs/` in the current directory:
```
logs/bender_20260126_111300.log
```

Log levels:
- Console: WARNING (INFO in visible mode)
- File: DEBUG (full details)

---

# 🇷🇺 Русский

# Bender - AI Task Supervisor

Bender супервайзер для AI CLI инструментов (GitHub Copilot, Droid, Codex). Он не решает задачи сам, а следит чтобы их правильно выполнили старшие модели.

## Концепция

```
Ты даёшь задачу → Bender анализирует сложность → Выбирает worker →
→ Мониторит выполнение → Пинает если застрял → Проверяет результат
```

## Установка

```bash
# 1. Клонировать
git clone https://github.com/DoroninDobroCorp/bender.git
cd bender

# 2. Установить как CLI
pip install -e .

# 3. Настроить .env
cp .env.example .env
# Заполнить GLM_API_KEY (Cerebras)

# 4. Проверить
bender --help
```

## Требования

- Python 3.10+
- macOS с Terminal.app (для interactive режима)
- tmux (для visible режима без -I)
- GitHub Copilot CLI (`copilot`)
- Опционально: `droid`, `codex`

## Быстрый старт

```bash
# Простой запуск - bender выберет worker автоматически
bender run "Добавь тест для модуля auth"

# Видимый режим - откроет терминал с copilot
bender run -v "Добавь тест для модуля auth"

# Интерактивный режим - полноценный терминал, можно продолжить вручную
bender run -vI "Добавь тест для модуля auth"

# Review loop - итеративный цикл до устранения всех проблем
bender run -lvI "Добавь OAuth авторизацию"
```

## Режимы работы

### 1. Стандартный режим (`bender run`)

```bash
bender run "задача"           # Авто-выбор worker'а
bender run --droid "задача"   # Принудительно droid (простое)
bender run --opus "задача"    # Принудительно copilot (среднее)
bender run --codex "задача"   # Принудительно codex (сложное)
```

### 2. Visible режим (`-v`)

Открывает окно терминала с tmux сессией:

```bash
bender run -v "задача"
```

- Видно что делает copilot
- Можно листать историю (scrollback)
- При завершении bender закрывает окно

### 3. Interactive режим (`-I`) ⭐ НОВОЕ

**Нативный терминал** — точно такой же как когда ты сам работаешь с copilot:

```bash
bender run -vI --simple "задача"
```

**Как работает:**
- Bender открывает **новое окно Terminal.app** (не tmux!)
- Запускает copilot с твоей задачей
- Терминал остаётся открытым пока работает — можно листать, скроллить
- После завершения терминал **автоматически закрывается**
- Bender автоматически отвечает на запросы разрешений (y/n)

**Преимущества:**
- Терминал ТОЧНО такой же как когда ты работаешь сам
- Полный scrollback — листай сколько хочешь
- Можно вмешаться в любой момент (пока работает)
- Без `--simple` — сначала анализирует задачу и добавляет критерии

### 4. Review Loop (`-l`)

Итеративный цикл: copilot выполняет → reviewer проверяет → copilot исправляет:

```bash
bender run -l "задача"        # Copilot → Codex → Copilot
bender run -lc "задача"       # Copilot → Copilot (экономит лимиты codex)
bender run -lvI "задача"      # С интерактивным терминалом
```

**Как работает:**
1. **Copilot** выполняет задачу
2. **Reviewer** дотошно проверяет (BMAD роли, визуально, тесты)
3. **GLM** анализирует findings:
   - `fix` — нужно исправить
   - `skip` — можно пропустить
   - `done` — всё готово
4. Если `fix` → новый Copilot с инструкциями
5. Повторять до `done` или max iterations

## Все опции

```
bender run [OPTIONS] [TASK]

Options:
  --droid               Принудительно droid (простые задачи)
  --opus                Принудительно copilot (средние задачи)
  --codex               Принудительно codex (сложные задачи)
  -a, --auto            Авто-выбор по сложности (по умолчанию)
  -i, --interval N      Интервал проверки логов в секундах (default: 60)
  -s, --simple          Без анализа и верификации
  -v, --visible         Показывать терминалы
  -I, --interactive     Интерактивный режим (полный терминал)
  -l, --review-loop     Итеративный цикл copilot→reviewer
  -c, --copilot-review  Использовать copilot вместо codex для review
  -d, --droid-mode      Использовать droid для execution и review
  --max-iterations N    Макс. итераций для review loop (default: 10)
  -C, --continue-errors Продолжить с ошибками (comma-separated)
  -E, --errors-interactive  Ввести ошибки интерактивно
  -p, --project PATH    Путь к проекту
  --help                Справка
```

## Примеры использования

```bash
# Простая задача
bender run "Исправь опечатку в README"

# Задача с видимым терминалом
bender run -v "Добавь unit тест для auth"

# Интерактивный режим - можно продолжить если bender упадёт
bender run -vI "Рефакторинг модуля payments"

# Review loop с copilot reviewer (экономит codex лимиты)
bender run -lvIc "Добавь OAuth авторизацию"

# Продолжить с известными ошибками
bender run -lvIc -C "MEDIUM: отсутствует валидация email" "Доделать форму"

# Полностью автоматический режим (без visible)
bender run -l "Добавь API endpoint для пользователей"
```

## Другие команды

```bash
bender status    # Статус текущей задачи
bender attach    # Присоединиться к tmux сессии
```

## Конфигурация (.env)

```env
# Обязательно
GLM_API_KEY=csk-...           # Cerebras API key

# Опционально (можно несколько через запятую)
GLM_API_KEYS=csk-key1,csk-key2,csk-key3

# Опционально
DROID_PROJECT_PATH=/path/to/project
AUTO_GIT_PUSH=true
```

## LLM

- **Model:** Qwen (`qwen-3-235b-a22b-instruct-2507`) via Cerebras
- Rate limit: 30 req/min на бесплатном тарифе
- Auto-retry с exponential backoff между ключами
- Поддержка ротации нескольких API ключей

**При 429 rate limit:** Используй `-s` (simple mode) — работает БЕЗ GLM анализа:
```bash
bender run -vIs "задача"     # Интерактивный без GLM
bender run -lvIs "задача"    # Review loop без GLM анализа
```

## Структура проекта

```
bender/
├── bender/              # Основной код
│   ├── glm_client.py    # GLM API клиент
│   ├── llm_router.py    # GLM + Qwen fallback
│   ├── review_loop.py   # Итеративный review цикл
│   ├── task_clarifier.py
│   ├── task_manager.py
│   ├── worker_manager.py
│   ├── log_watcher.py
│   └── workers/
│       ├── base.py              # Базовый worker
│       ├── copilot.py           # Copilot (non-interactive)
│       ├── interactive_copilot.py  # Copilot (interactive) ⭐
│       ├── droid.py
│       └── codex.py
├── bender_cli/          # CLI интерфейс
│   └── main.py
├── core/                # Конфигурация
│   ├── config.py
│   └── logging_config.py
└── tests/
```

## Логи

Логи сохраняются в `logs/` в текущей директории:
```
logs/bender_20260126_111300.log
```

Уровни логирования:
- Console: WARNING (INFO в visible режиме)
- File: DEBUG (полная информация)

WINDOW-TEST
