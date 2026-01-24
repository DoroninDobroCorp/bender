# Bender - AI Task Supervisor

Bender супервайзер для AI CLI инструментов (copilot, droid, codex). Он не решает задачи сам, а следит чтобы их правильно выполнили старшие модели.

## Концепция

```
Ты даёшь задачу → Bender анализирует сложность → Выбирает worker →
→ Мониторит выполнение → Пинает если застрял → Проверяет результат
```

## Быстрый старт

```bash
# 1. Клонировать
git clone https://github.com/DoroninDobroCorp/bender.git
cd bender

# 2. Установить зависимости
pip install -r requirements.txt

# 3. Настроить .env
cp .env.example .env
# Заполнить GLM_API_KEY (Cerebras) и DROID_PROJECT_PATH

# 4. Запустить
python -m cli.main run "твоя задача"
```

## Примеры

```bash
# Авто-выбор worker'а по сложности
python -m cli.main run "исправь опечатку в README"     # → droid (simple)
python -m cli.main run "добавь unit тест для auth"     # → opus (medium)
python -m cli.main run "добавь OAuth авторизацию"      # → codex (complex)

# Принудительный выбор worker'а
python -m cli.main run --droid "простая задача"
python -m cli.main run --codex "сложная задача"

# Простой режим (без анализа и верификации)
python -m cli.main run --simple "echo hello"

# Видимый режим (терминалы открыты для отладки)
python -m cli.main run --visible "задача"

# Кастомный интервал проверки логов
python -m cli.main run --interval 10 "задача"
```

## Как работает

```
┌─────────────────────────────────────────────────┐
│  bender run "Добавь OAuth"                      │
└───────────────────┬─────────────────────────────┘
                    ▼
┌─────────────────────────────────────────────────┐
│  TaskClarifier (GLM)                            │
│  • Анализ сложности: SIMPLE/MEDIUM/COMPLEX      │
│  • Генерация acceptance criteria                │
│  • Уточняющие вопросы если нужно                │
└───────────────────┬─────────────────────────────┘
                    ▼
         ┌──────────┴──────────┐
         ▼          ▼          ▼
      SIMPLE     MEDIUM     COMPLEX
       droid      opus       codex
     (no verif)            (x2 interval)
         │          │          │
         └──────────┴──────────┘
                    ▼
┌─────────────────────────────────────────────────┐
│  Мониторинг + NUDGE                             │
│  • Захват логов каждые N секунд                 │
│  • GLM анализирует: stuck/loop/completed        │
│  • "Все пункты ТЗ выполнены?" если застрял      │
│  • Restart только если сессия умерла            │
└───────────────────┬─────────────────────────────┘
                    ▼
          [COMPLEX + много изменений?]
                    │
                    ▼
           Codex final review
           (поиск багов)
```

## Workers

| Worker | CLI | Когда | Особенности |
|--------|-----|-------|-------------|
| **droid** | `droid` | Простые задачи | Без верификации |
| **opus** | `copilot --allow-all-tools` | Основной режим | Полный цикл |
| **codex** | `codex --dangerous-mode` | Сложные задачи | Интервал x2, final review |

## Конфигурация (.env)

```env
# Обязательно
GLM_API_KEY=csk-...           # Cerebras API key
DROID_PROJECT_PATH=/path/to/project

# Опционально
DROID_BINARY=droid
AUTO_GIT_PUSH=true
DISPLAY_MODE=visible
```

## LLM

- **Primary:** GLM (Cerebras `zai-glm-4.7`) - thinking model с reasoning
- **Fallback:** Qwen (`qwen-3-235b-a22b-instruct-2507`)

## Структура

```
bender/
├── bender/
│   ├── glm_client.py      # GLM API клиент
│   ├── llm_router.py      # GLM + Qwen fallback
│   ├── task_clarifier.py  # Анализ задачи
│   ├── task_manager.py    # Управление выполнением
│   ├── worker_manager.py  # Управление workers
│   ├── log_watcher.py     # Анализ логов
│   └── workers/           # Copilot, Droid, Codex workers
├── cli/
│   └── main.py            # CLI интерфейс
├── core/
│   └── config.py          # Конфигурация
└── tests/
```
