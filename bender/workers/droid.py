"""
Droid Worker - worker для Droid CLI (простые задачи)

НАДЁЖНАЯ ДЕТЕКЦИЯ:
- Используем `droid exec` с exit code
- Процесс завершается сам когда задача готова
- Fallback на паттерны для интерактивного режима

ПРОЗРАЧНЫЙ ВЫВОД:
- stream-json формат показывает все шаги в реальном времени
- Парсим JSON события и форматируем для читаемости

STREAMING SUPPORT:
- event_stream() маппит JSONL события на UniversalEvent
- DroidAdapter используется для fallback (non-JSON строки)
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from collections.abc import AsyncIterator
from typing import List, Optional, Tuple, Callable, Awaitable

from .base import BaseWorker, WorkerConfig, WorkerStatus
from backend.core.events.schema import EventType, UniversalEvent

logger = logging.getLogger(__name__)


class DroidWorker(BaseWorker):
    """Worker для Droid CLI
    
    Надёжная детекция завершения:
    1. Non-interactive: `droid exec` завершается сам
    2. Interactive: паттерны + exit code процесса
    """
    
    WORKER_NAME = "droid"
    INTERVAL_MULTIPLIER = 1.0
    
    # Паттерны завершения (для интерактивного режима)
    COMPLETION_PATTERNS = [
        "Task completed",
        "All done", 
        "Successfully",
        "Готово",
        "Changes saved",
        "File updated",
        "## Summary",  # Droid часто выводит summary в конце
    ]

    PTY_ERROR_PATTERNS = [
        "forkpty: Device not configured",
        "Could not create a new process and open a pseudo-tty",
        "pty_posix_spawn failed",
        "PTY not available",
        "openpty",
    ]
    
    def __init__(
        self,
        config: WorkerConfig,
        llm_check_completion: Optional[Callable[[str, str], Awaitable[bool]]] = None,
        llm_analyze: Optional[Callable[[str, str, float], Awaitable[dict]]] = None,
    ):
        super().__init__(config)
        self._output: str = ""
        self._formatted_output: str = ""  # Форматированный вывод для отображения
        self._completed: bool = False
        self._llm_analyze = llm_analyze
        self._current_task_text = ""
        self._process: Optional[subprocess.Popen] = None
        self._last_log_size: int = 0
        self._last_log_update_ts: float = 0.0
        self._last_parsed_line: int = 0  # Последняя распарсенная строка
        self._streaming_adapter = None  # Lazy init
        self._session_started = False
        self._session_ended = False
    
    @property
    def cli_command(self) -> List[str]:
        # Всегда используем exec для надёжного завершения
        # --auto high даёт полную автономию
        # stream-json для прозрачного вывода всех шагов
        return [
            "droid", "exec",
            "--auto", "high",
            "--output-format", "stream-json",
        ]
    
    def format_task(self, task: str, context: Optional[str] = None) -> str:
        self._current_task_text = task
        if context:
            return f"{task}\n\nПредыдущий контекст:\n{context}"
        return task
    
    def _parse_stream_json_events(self, raw_output: str) -> str:
        """Парсит stream-json события и форматирует для читаемости
        
        Args:
            raw_output: Сырой вывод с JSONL событиями
            
        Returns:
            Форматированный текст для отображения
        """
        lines = raw_output.strip().split('\n')
        formatted_parts = []
        
        # Парсим только новые строки (с последней обработанной)
        for i, line in enumerate(lines[self._last_parsed_line:], start=self._last_parsed_line):
            if not line.strip():
                continue
                
            try:
                event = json.loads(line)
                event_type = event.get('type')
                
                if event_type == 'system':
                    # Инициализация - показываем модель и инструменты
                    model = event.get('model', 'unknown')
                    tools_count = len(event.get('tools', []))
                    formatted_parts.append(f"🤖 Инициализация: {model} ({tools_count} инструментов)")
                
                elif event_type == 'message':
                    role = event.get('role')
                    text = event.get('text', '')
                    
                    if role == 'user':
                        # Промпт пользователя
                        formatted_parts.append(f"\n📝 Задача: {text[:200]}{'...' if len(text) > 200 else ''}")
                    elif role == 'assistant':
                        # Мысли ассистента
                        if text and len(text) > 10:  # Игнорируем короткие технические сообщения
                            formatted_parts.append(f"\n💭 Дроид: {text}")
                
                elif event_type == 'tool_call':
                    # Вызов инструмента
                    tool_name = event.get('toolName', 'Unknown')
                    params = event.get('parameters', {})
                    
                    # Форматируем параметры в зависимости от инструмента
                    if tool_name == 'Execute':
                        cmd = params.get('command', '')
                        formatted_parts.append(f"\n🔧 Выполняю: {cmd}")
                    elif tool_name == 'Read':
                        file_path = params.get('file_path', '')
                        formatted_parts.append(f"\n📖 Читаю: {file_path}")
                    elif tool_name == 'Edit':
                        file_path = params.get('file_path', '')
                        formatted_parts.append(f"\n✏️  Редактирую: {file_path}")
                    elif tool_name == 'Create':
                        file_path = params.get('file_path', '')
                        formatted_parts.append(f"\n📄 Создаю: {file_path}")
                    elif tool_name == 'Grep':
                        pattern = params.get('pattern', '')
                        formatted_parts.append(f"\n🔍 Ищу: {pattern}")
                    else:
                        formatted_parts.append(f"\n🔧 {tool_name}: {str(params)[:100]}")
                
                elif event_type == 'tool_result':
                    # Результат инструмента
                    is_error = event.get('isError', False)
                    value = event.get('value', '')
                    
                    if is_error:
                        formatted_parts.append(f"   ❌ Ошибка: {value[:200]}")
                    else:
                        # Показываем краткий результат
                        if isinstance(value, str) and len(value) > 300:
                            formatted_parts.append(f"   ✅ Результат: {value[:300]}...")
                        else:
                            formatted_parts.append(f"   ✅ Результат: {value}")
                
                elif event_type == 'completion':
                    # Финальный результат
                    final_text = event.get('finalText', '')
                    duration_ms = event.get('durationMs', 0)
                    num_turns = event.get('numTurns', 0)
                    
                    formatted_parts.append(f"\n\n{'='*60}")
                    formatted_parts.append(f"✅ ЗАВЕРШЕНО за {duration_ms/1000:.1f}с ({num_turns} шагов)")
                    formatted_parts.append(f"{'='*60}")
                    formatted_parts.append(f"\n{final_text}")
                
            except json.JSONDecodeError:
                # Не JSON строка - возможно обычный текст или ошибка
                if line.strip() and not line.startswith('{'):
                    formatted_parts.append(line)
            except Exception as e:
                logger.debug(f"[{self.WORKER_NAME}] Error parsing event: {e}")
        
        # Обновляем счётчик обработанных строк
        self._last_parsed_line = len(lines)
        
        return '\n'.join(formatted_parts)

    async def _capture_tmux_output(self) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                "tmux", "capture-pane", "-t", self.session_id, "-p", "-S", "-1000",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await process.communicate()
            return stdout.decode("utf-8", errors="replace")
        except Exception:
            return ""

    async def capture_output(self) -> str:
        """Droid: парсим stream-json и возвращаем форматированный вывод."""
        if self.config.visible:
            return await super().capture_output()
        
        raw_output = ""
        
        if self._log_file is not None and self._log_file.exists():
            try:
                raw_output = self._log_file.read_text(errors='replace')
            except Exception:
                raw_output = ""
            
            if raw_output:
                now = time.time()
                if len(raw_output) != self._last_log_size:
                    self._last_log_size = len(raw_output)
                    self._last_log_update_ts = now
                    
                    # Парсим JSON события и форматируем
                    self._output = raw_output  # Сохраняем сырой вывод
                    new_formatted = self._parse_stream_json_events(raw_output)
                    if new_formatted:
                        self._formatted_output += new_formatted
                    
                    return self._formatted_output
                
                # Лог завис (нет обновлений) — попробуем tmux capture
                # ВАЖНО: tmux может иметь более свежий контент чем лог файл
                if now - self._last_log_update_ts > 2.0:
                    tmux_output = await self._capture_tmux_output()
                    if tmux_output and len(tmux_output) > len(raw_output):
                        # Tmux имеет больше данных - используем его
                        self._output = tmux_output
                        self._last_log_size = len(tmux_output)
                        new_formatted = self._parse_stream_json_events(tmux_output)
                        if new_formatted:
                            self._formatted_output += new_formatted
                        return self._formatted_output
                
                return self._formatted_output
        
        return await super().capture_output()
    
    def _detect_completion_from_json(self, raw_output: str) -> bool:
        """Детектирует завершение из stream-json событий
        
        Args:
            raw_output: Сырой вывод с JSONL событиями
            
        Returns:
            True если найдено событие completion
        """
        lines = raw_output.strip().split('\n')
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                if event.get('type') == 'completion':
                    return True
            except json.JSONDecodeError:
                continue
        return False
    
    async def wait_for_completion(self, timeout: float = 600) -> Tuple[bool, str]:
        """Дождаться завершения
        
        Для stream-json режима - ждём событие completion
        Fallback: exit code процесса
        
        ВАЖНО: Возвращаем RAW output (_output), а не formatted (_formatted_output)!
        """
        
        start = asyncio.get_event_loop().time()
        check_interval = 10  # Проверка каждые 10 секунд
        raw_output = ""
        
        while asyncio.get_event_loop().time() - start < timeout:
            await asyncio.sleep(check_interval)
            elapsed = asyncio.get_event_loop().time() - start
            
            # Читаем сырой вывод
            if self._log_file is not None and self._log_file.exists():
                try:
                    raw_output = self._log_file.read_text(errors='replace')
                    self._output = raw_output  # Сохраняем для property
                except Exception:
                    raw_output = ""
            else:
                # Получаем форматированный вывод через capture_output
                formatted = await self.capture_output()
                raw_output = self._output  # Сырой вывод сохранён в _output
            
            # 0. Ошибки PTY — если они появились, сразу фейлим
            if raw_output:
                last_chunk = raw_output[-3000:] if len(raw_output) > 3000 else raw_output
                if any(p in last_chunk for p in self.PTY_ERROR_PATTERNS):
                    self._completed = True
                    self.status = WorkerStatus.ERROR
                    logger.error(f"[{self.WORKER_NAME}] PTY error detected in output")
                    # Возвращаем RAW output, не formatted!
                    return False, self._output if self._output else self._formatted_output

            # 1. НАДЁЖНО: Проверяем событие completion в stream-json
            if self._detect_completion_from_json(raw_output):
                self._completed = True
                self.status = WorkerStatus.COMPLETED
                logger.info(f"[{self.WORKER_NAME}] Completion event detected")
                return True, self._formatted_output

            # 2. НАДЁЖНО: Проверяем завершился ли процесс
            session_alive = await self.is_session_alive()
            if not session_alive:
                # Проверяем exit code из done файла
                exit_code = None
                done_file = getattr(self, '_done_file', None)
                if done_file and done_file.exists():
                    try:
                        exit_code = int(done_file.read_text().strip())
                    except Exception:
                        exit_code = None
                self._completed = True
                if exit_code is not None and exit_code != 0:
                    self.status = WorkerStatus.ERROR
                    logger.warning(f"[{self.WORKER_NAME}] Process exited with code {exit_code}")
                    return False, self._formatted_output
                self.status = WorkerStatus.COMPLETED
                logger.info(f"[{self.WORKER_NAME}] Process exited - task completed")
                return True, self._formatted_output
            
            # 3. Детекция зависания (для droid не фейлим на тишину, только логируем)
            if self.detect_stuck(raw_output):
                # droid часто молчит в exec-режиме — не считаем это ошибкой
                logger.info(f"[{self.WORKER_NAME}] No output for 5min, continuing (droid can be silent)")
                continue
        
        # Таймаут
        self.status = WorkerStatus.TIMEOUT
        logger.warning(f"[{self.WORKER_NAME}] Timeout after {timeout}s")
        # Возвращаем RAW output, не formatted!
        return False, self._output if self._output else self._formatted_output
    
    @property
    def output(self) -> str:
        """Возвращает RAW вывод для review (не форматированный!)
        
        ВАЖНО: Для review нужен ПОЛНЫЙ сырой текст, а не красиво отформатированный.
        Форматированный вывод (_formatted_output) теряет данные при парсинге JSON событий.
        """
        return self._output if self._output else self._formatted_output
    
    @property
    def completed(self) -> bool:
        return self._completed

    def _create_streaming_adapter(self):
        """DroidWorker использует DroidAdapter для streaming."""
        from backend.core.events.adapters.droid_adapter import DroidAdapter
        return DroidAdapter()

    def _map_jsonl_to_event(self, json_event: dict) -> Optional[UniversalEvent]:
        """Маппит одно JSONL событие на UniversalEvent.

        Args:
            json_event: Распарсенное JSON событие из stream-json

        Returns:
            UniversalEvent или None если событие не маппится
        """
        if self._streaming_adapter is None:
            return None

        event_type_str = json_event.get('type')

        # Mapping JSONL event type → EventType
        if event_type_str == 'system':
            # system → SESSION_START
            model = json_event.get('model', 'unknown')
            tools = json_event.get('tools', [])
            return self._streaming_adapter._make_event(
                EventType.SESSION_START,
                {"model": model, "tools_count": len(tools), "tools": tools}
            )

        elif event_type_str == 'message':
            # message → ITEM_DELTA (для assistant) или игнорируем user
            role = json_event.get('role')
            text = json_event.get('text', '')

            if role == 'assistant' and text and len(text) > 10:
                return self._streaming_adapter._make_event(
                    EventType.ITEM_DELTA,
                    {"role": role, "text": text}
                )
            # user messages игнорируем (это промпт)
            return None

        elif event_type_str == 'tool_call':
            # tool_call → ITEM_START
            tool_name = json_event.get('toolName', 'Unknown')
            parameters = json_event.get('parameters', {})
            return self._streaming_adapter._make_event(
                EventType.ITEM_START,
                {"toolName": tool_name, "parameters": parameters}
            )

        elif event_type_str == 'tool_result':
            # tool_result → ITEM_END
            value = json_event.get('value', '')
            is_error = json_event.get('isError', False)
            return self._streaming_adapter._make_event(
                EventType.ITEM_END,
                {"value": value, "isError": is_error}
            )

        elif event_type_str == 'completion':
            # completion → TASK_COMPLETED
            final_text = json_event.get('finalText', '')
            duration_ms = json_event.get('durationMs', 0)
            num_turns = json_event.get('numTurns', 0)
            return self._streaming_adapter._make_event(
                EventType.TASK_COMPLETED,
                {
                    "finalText": final_text,
                    "durationMs": duration_ms,
                    "numTurns": num_turns
                }
            )

        return None

    async def event_stream(self) -> AsyncIterator[UniversalEvent]:
        """Stream UniversalEvents из Droid JSONL output.

        Парсит stream-json события и маппит их на UniversalEvent типы:
        - system → SESSION_START
        - message → ITEM_DELTA
        - tool_call → ITEM_START
        - tool_result → ITEM_END
        - completion → TASK_COMPLETED

        Fallback: non-JSON строки обрабатываются через DroidAdapter.feed_text()

        Yields:
            UniversalEvent для каждого события
        """
        if self._streaming_adapter is None:
            self._streaming_adapter = self._create_streaming_adapter()

        # Start session
        agent_id = f"{self.WORKER_NAME}-{self.session_id}"
        session_event = self._streaming_adapter.start_session(
            agent_id=agent_id,
            session_id=self.session_id,
            user_id=None
        )
        self._session_started = True
        yield session_event

        last_read_offset = 0

        while True:
            # Читаем лог файл инкрементально
            if self._log_file is None or not self._log_file.exists():
                await asyncio.sleep(1.0)

                # Проверяем завершился ли процесс
                if not await self.is_session_alive():
                    break
                continue

            try:
                content = self._log_file.read_text(errors='replace')
            except Exception as e:
                logger.warning(f"[{self.WORKER_NAME}] Error reading log: {e}")
                await asyncio.sleep(1.0)
                continue

            # Парсим новые строки (с последнего offset)
            if len(content) <= last_read_offset:
                await asyncio.sleep(1.0)

                # Проверяем завершился ли процесс
                if not await self.is_session_alive():
                    break
                continue

            new_chunk = content[last_read_offset:]
            last_read_offset = len(content)

            # Разбиваем на строки
            lines = new_chunk.split('\n')

            for line in lines:
                if not line.strip():
                    continue

                try:
                    # Пробуем распарсить как JSON
                    json_event = json.loads(line)

                    # Маппим на UniversalEvent
                    event = self._map_jsonl_to_event(json_event)
                    if event:
                        yield event

                        # Если это completion, выходим
                        if event.event_type == EventType.TASK_COMPLETED:
                            self._completed = True
                            # Отправляем SESSION_END после TASK_COMPLETED
                            end_events = self._streaming_adapter.end_session()
                            for e in end_events:
                                yield e
                            self._session_ended = True
                            return

                except json.JSONDecodeError:
                    # Не JSON строка - используем fallback через adapter
                    if line.strip() and not line.startswith('{'):
                        events = self._streaming_adapter.feed_text(line + '\n')
                        for e in events:
                            yield e
                except Exception as e:
                    logger.debug(f"[{self.WORKER_NAME}] Error parsing event: {e}")

            # Проверяем завершился ли процесс через _detect_completion_from_json
            if self._detect_completion_from_json(content):
                self._completed = True
                break

            # Проверяем завершился ли процесс
            if not await self.is_session_alive():
                break

            await asyncio.sleep(1.0)

        # Завершаем сессию если не завершили ранее
        if not self._session_ended and self._session_started:
            end_events = self._streaming_adapter.end_session()
            for e in end_events:
                yield e
            self._session_ended = True
