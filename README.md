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
