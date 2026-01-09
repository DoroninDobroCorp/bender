# Parser Maker

Автоматизация создания парсеров через LLM с 6-шаговым pipeline и supervisor Bender (Gemini).

## Концепция

Каждый парсер создаётся в 6 последовательных шагов. На каждом шаге:
1. Bender даёт Droid ТЗ шага
2. Droid работает и отвечает: "сделал изменения X, Y, Z" или "всё ок, ничего не менял"
3. Bender анализирует ответ и решает что дальше

**Правило перехода:** Droid должен **дважды подряд** сказать "всё ок, без существенных изменений" - тогда переходим на следующий шаг.

**Git:** После каждой итерации с существенными изменениями - автоматический commit и push.

## Роли

| Компонент | Кто | Что делает |
|-----------|-----|------------|
| **Droid** | Factory Droid (дорогая модель) | Выполняет работу, сам говорит сделал ли изменения |
| **Bender** | Gemini | Следит за Droid, анализирует ответы, помогает как программист |

## Что делает Bender

Bender - это программист-помощник на Gemini который:
- **Следит** за здоровьем Droid (не завис? не зациклился? не вылетел?)
- **Понимает** ответы Droid (были изменения? существенные? ТЗ выполнено?)
- **Настаивает** на завершении ТЗ если Droid не закончил
- **Помогает** если проблема (перезапуск, новый чат, пинок)
- **Эскалирует** к человеку только в крайнем случае

## Быстрый старт

```bash
# Установка
pip install -r requirements.txt
cp .env.example .env
# Заполнить .env (GEMINI_API_KEY обязателен)

# Запуск (visible mode - видно всё)
parser-maker run /path/to/project

# Запуск (silent mode - только прогресс)
parser-maker run /path/to/project --silent

# Продолжить после сбоя
parser-maker resume

# Статус
parser-maker status
```

## Режимы отображения

### Visible (по умолчанию)
Видно всё: мысли Bender, output Droid, git операции, решения.

### Silent
Только прогресс: "Шаг 2/6, итерация 3" и финальный результат.

## Документация

- [Архитектура](docs/ARCHITECTURE.md) - как устроена система, flow, компоненты
- [Epics & Stories](docs/EPICS.md) - план разработки

## Конфигурация

```env
# Обязательно
GEMINI_API_KEY=your_key
DROID_PROJECT_PATH=/path/to/project

# Опционально
CEREBRAS_API_KEY=your_key  # fallback при недоступности Gemini
DROID_BINARY=droid
AUTO_GIT_PUSH=true
DISPLAY_MODE=visible  # visible или silent
BENDER_ESCALATE_AFTER=5  # после скольких неудач спрашивать человека
WATCHDOG_INTERVAL=300  # секунд между проверками (default: 300 = 5 мин)
WATCHDOG_TIMEOUT=3600  # общий таймаут на задачу (default: 3600 = 1 час)
```

## Структура проекта

```
parser_maker/
├── core/           # Droid Controller, Config
├── bender/         # Gemini + GLM supervisor (watchdog, analyzer, enforcer)
├── pipeline/       # 6-step pipeline, git manager
├── state/          # Persistence, recovery
├── cli/            # CLI interface, display modes
├── steps/          # Конфигурация 6 шагов (YAML)
└── integrations/   # Telegram notifications, desktop fallback
```
