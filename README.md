# Bender - AI Task Supervisor

Bender супервайзер для AI CLI инструментов (copilot, droid, codex). Он не решает задачи сам, а следит чтобы их правильно выполнили старшие модели.

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

## Использование

### Базовые команды

```bash
# Авто-выбор worker'а по сложности
bender run "исправь опечатку в README"     # → droid (simple)
bender run "добавь unit тест для auth"     # → opus/copilot (medium)
bender run "добавь OAuth авторизацию"      # → codex (complex)

# Принудительный выбор worker'а
bender run --droid "простая задача"
bender run --opus "средняя задача"
bender run --codex "сложная задача"

# Без анализа и верификации (быстро)
bender run -s "задача"
bender run --simple "задача"

# С видимыми терминалами (для отладки)
bender run -v "задача"
bender run --visible "задача"
```

### Review Loop (итеративный цикл)

Итеративный цикл copilot → reviewer → copilot до устранения всех проблем:

```bash
# Copilot выполняет → Codex проверяет → Copilot исправляет → ...
bender run -l "задача"
bender run --review-loop "задача"

# С copilot вместо codex для review (экономит лимиты codex)
bender run -lc "задача"
bender run --review-loop --copilot-review "задача"

# Ограничить количество итераций (по умолчанию 10)
bender run -l --max-iterations 5 "задача"

# Комбинировать с visible mode
bender run -lcv "задача"
```

**Логика Review Loop:**
1. **Copilot** выполняет задачу
2. **Reviewer (codex или copilot)** дотошно проверяет:
   - Код на баги и уязвимости
   - Визуально (скриншоты если нужно)
   - Каждая BMAD роль отдельно
3. **GLM** анализирует findings и решает:
   - `fix` — нужно исправить (CRITICAL/HIGH обязательно, MEDIUM/LOW на усмотрение)
   - `skip` — можно пропустить
   - `done` — всё готово
4. Если `fix` → **новый Copilot** с инструкциями
5. Повторять до `done` или max iterations

### Все опции

```
bender run [OPTIONS] TASK

Options:
  --droid               Принудительно droid (простые задачи)
  --opus                Принудительно copilot (средние задачи)
  --codex               Принудительно codex (сложные задачи)
  -a, --auto            Авто-выбор по сложности (по умолчанию)
  -i, --interval N      Интервал проверки логов в секундах (default: 30)
  -s, --simple          Без анализа и верификации
  -v, --visible         Показывать терминалы
  -l, --review-loop     Итеративный цикл copilot→reviewer
  -c, --copilot-review  Использовать copilot вместо codex для review
  --max-iterations N    Макс. итераций для review loop (default: 10)
  -p, --project PATH    Путь к проекту
  --help                Справка
```

### Другие команды

```bash
bender status          # Статус текущей задачи
bender stop            # Остановить выполнение
bender config          # Показать конфигурацию
bender test-glm        # Тест GLM соединения
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
           Review Loop (если -l)
           copilot → codex → copilot
```

## Workers

| Worker | CLI | Когда | Особенности |
|--------|-----|-------|-------------|
| **droid** | `droid` | Простые задачи | Без верификации |
| **opus** | `gh copilot-chat --allow-all-tools` | Средние задачи | Полный цикл |
| **codex** | `codex --dangerously-bypass-approvals-and-sandbox` | Сложные задачи | Интервал x2 |

## Конфигурация (.env)

```env
# Опционально (для GLM)
GLM_API_KEY=csk-...           # Cerebras API key

# Опционально
DROID_PROJECT_PATH=/path/to/project
DROID_BINARY=droid
AUTO_GIT_PUSH=true
DISPLAY_MODE=visible
```

## LLM

- **Primary:** GLM (Cerebras `zai-glm-4.7`) — thinking model
- **Fallback:** Qwen (`qwen-3-235b-a22b-instruct-2507`) — 235B параметров
- Rate limit: 30 req/min, auto-retry с exponential backoff

## Структура

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
│   └── workers/         # Copilot, Droid, Codex
├── bender_cli/          # CLI интерфейс
│   └── main.py
├── core/                # Конфигурация и утилиты
│   ├── config.py
│   └── logging_config.py
└── tests/
```

## Логи

Логи сохраняются в `logs/` в текущей директории:
```
logs/bender_20260126_111300.log
```
