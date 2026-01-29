"""
Worker Manager - управление CLI workers
"""

import asyncio
import logging
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Type, Callable, Awaitable, List

from .workers.base import BaseWorker, WorkerConfig, WorkerStatus, WorkerResult
from .workers.copilot import CopilotWorker
from .workers.interactive_copilot import InteractiveCopilotWorker
from .workers.droid import DroidWorker
from .workers.codex import CodexWorker
from .glm_client import clean_surrogates

logger = logging.getLogger(__name__)


def cleanup_stale_bender_sessions() -> List[str]:
    """Убить старые bender tmux сессии (без активных)
    
    Вызывается при старте чтобы очистить зависшие сессии
    от предыдущих запусков.
    
    Returns:
        List of killed session names
    """
    killed: List[str] = []
    max_age_seconds = 6 * 3600  # 6 hours
    min_idle_seconds = 30 * 60  # 30 minutes
    try:
        # Получить список сессий
        result = subprocess.run(
            ['tmux', 'list-sessions', '-F', '#{session_name}\t#{session_attached}\t#{session_created}\t#{session_activity}'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode != 0:
            return killed
        
        sessions = [s for s in result.stdout.strip().split('\n') if s.strip()]
        now = int(__import__('time').time())
        for session in sessions:
            parts = session.split('\t')
            if not parts:
                continue
            name = parts[0]
            if not name.startswith('bender-'):
                continue
            try:
                attached = int(parts[1]) if len(parts) > 1 else 0
                created = int(parts[2]) if len(parts) > 2 else now
                activity = int(parts[3]) if len(parts) > 3 else created
            except ValueError:
                # Если формат не распарсили — безопасно пропускаем
                continue
            
            if attached > 0:
                continue
            
            age = now - created
            idle = now - activity
            if age < max_age_seconds and idle < min_idle_seconds:
                continue
            
            try:
                subprocess.run(
                    ['tmux', 'kill-session', '-t', name],
                    capture_output=True,
                    timeout=5
                )
                killed.append(name)
                logger.info(f"Killed stale tmux session: {name}")
            except Exception as e:
                logger.warning(f"Failed to kill session {name}: {e}")
    except FileNotFoundError:
        pass  # tmux not installed
    except subprocess.TimeoutExpired:
        logger.warning("Timeout listing tmux sessions")
    except Exception as e:
        logger.warning(f"Error cleaning up tmux sessions: {e}")
    
    return killed


class WorkerType(str, Enum):
    """Типы workers"""
    OPUS = "opus"                    # copilot с opus (default, -p режим)
    OPUS_INTERACTIVE = "opus_interactive"  # copilot интерактивный (новый!)
    DROID = "droid"                  # droid для простых задач
    CODEX = "codex"                  # codex для сложных задач


WORKER_CLASSES: Dict[WorkerType, Type[BaseWorker]] = {
    WorkerType.OPUS: CopilotWorker,
    WorkerType.OPUS_INTERACTIVE: InteractiveCopilotWorker,
    WorkerType.DROID: DroidWorker,
    WorkerType.CODEX: CodexWorker,
}


@dataclass
class ManagerConfig:
    """Конфигурация WorkerManager"""
    project_path: Path
    check_interval: float = 60.0
    visible: bool = False
    simple_mode: bool = False
    max_retries: int = 3
    stuck_timeout: float = 300.0
    interactive_mode: bool = False  # Использовать интерактивный copilot
    status_interval: float = 30.0   # Интервал статуса для интерактивного режима
    log_watcher: Optional[object] = None  # LogWatcher для человеко-читаемых статусов


class WorkerManager:
    """Менеджер CLI workers
    
    Управляет жизненным циклом workers, следит за их состоянием,
    перезапускает при необходимости.
    """
    
    _cleanup_done = False  # Class-level flag to cleanup only once per process
    
    def __init__(
        self,
        config: ManagerConfig,
        on_output: Optional[Callable[[str], Awaitable[None]]] = None,
        on_status_change: Optional[Callable[[WorkerStatus], Awaitable[None]]] = None,
        on_status: Optional[Callable[[str], Awaitable[None]]] = None,  # Для интерактивного режима
        on_question: Optional[Callable[[str], Awaitable[Optional[str]]]] = None,  # Вопросы от copilot
        cleanup_stale: bool = True,
        llm_check_completion: Optional[Callable[[str, str], Awaitable[bool]]] = None,
        llm_analyze: Optional[Callable[[str, str, float], Awaitable[dict]]] = None,  # LLM анализ логов
    ):
        self.config = config
        self.on_output = on_output
        self.on_status_change = on_status_change
        self.on_status = on_status  # Callback для статуса (текстовый)
        self.on_question = on_question  # Callback для вопросов
        self._llm_check_completion = llm_check_completion  # LLM для проверки завершения (droid)
        self._llm_analyze = llm_analyze  # LLM для анализа логов (codex)
        
        self._current_worker: Optional[BaseWorker] = None
        self._watch_task: Optional[asyncio.Task] = None
        self._last_output: str = ""
        
        # Cleanup stale sessions once per process
        if cleanup_stale and not WorkerManager._cleanup_done:
            killed = cleanup_stale_bender_sessions()
            if killed:
                logger.info(f"Cleaned up {len(killed)} stale bender sessions")
            WorkerManager._cleanup_done = True
    
    @property
    def current_worker(self) -> Optional[BaseWorker]:
        return self._current_worker
    
    @property
    def is_running(self) -> bool:
        return self._current_worker is not None and self._current_worker.status == WorkerStatus.RUNNING
    
    def _create_worker(self, worker_type: WorkerType) -> BaseWorker:
        """Создать worker нужного типа"""
        logger.info(f"Creating worker with project_path: {self.config.project_path}")
        worker_config = WorkerConfig(
            project_path=self.config.project_path,
            check_interval=self.config.check_interval,
            visible=self.config.visible,
            simple_mode=self.config.simple_mode,
            max_retries=self.config.max_retries,
            stuck_timeout=self.config.stuck_timeout,
        )
        
        worker_class = WORKER_CLASSES[worker_type]
        
        # Для CopilotWorker передаём visible и LLM analyze
        if worker_type == WorkerType.OPUS:
            return worker_class(
                worker_config, 
                visible=self.config.visible,
                llm_analyze=self._llm_analyze,
            )
        
        # Для InteractiveCopilotWorker передаём все callback'и
        if worker_type == WorkerType.OPUS_INTERACTIVE:
            return worker_class(
                worker_config,
                on_status=self.on_status,
                on_question=self.on_question,
                auto_allow_tools=True,  # Автоматически разрешаем tools
                status_interval=self.config.status_interval,
                log_watcher=self.config.log_watcher,  # Для человеко-читаемых статусов
            )
        
        # Для DroidWorker передаём LLM callbacks
        if worker_type == WorkerType.DROID:
            return worker_class(
                worker_config, 
                llm_check_completion=self._llm_check_completion,
                llm_analyze=self._llm_analyze,
            )
        
        # Для CodexWorker передаём LLM analyze callback
        if worker_type == WorkerType.CODEX:
            return worker_class(worker_config, llm_analyze=self._llm_analyze)
        
        return worker_class(worker_config)
    
    async def start_task(
        self,
        task: str,
        worker_type: WorkerType = WorkerType.OPUS,
        context: Optional[str] = None
    ) -> None:
        """Запустить задачу с указанным worker'ом"""
        # Остановить текущий worker если есть
        if self._current_worker:
            await self.stop()
        
        # Создать и запустить новый worker
        self._current_worker = self._create_worker(worker_type)
        await self._current_worker.start(task, context)
        
        # Для workers с wait_for_completion - не нужен watch loop
        # Для других workers - запустить мониторинг
        if not hasattr(self._current_worker, 'wait_for_completion'):
            self._watch_task = asyncio.create_task(self._watch_loop())
        
        logger.info(f"Task started with {worker_type.value} worker")
    
    async def wait_for_completion(self, timeout: float = 300) -> tuple:
        """Дождаться завершения задачи (для Copilot worker)"""
        if not self._current_worker:
            return False, ""
        
        if hasattr(self._current_worker, 'wait_for_completion'):
            success, output = await self._current_worker.wait_for_completion(timeout)
            return success, clean_surrogates(output)
        
        # Для других workers - просто ждём
        return False, "Worker does not support wait_for_completion"
    
    async def send_next_task(self, task: str, context: Optional[str] = None) -> None:
        """Отправить следующую задачу в текущую сессию (для interactive mode)
        
        Используется для итеративной работы - не создаём новую сессию,
        а продолжаем в текущей.
        """
        if not self._current_worker:
            raise RuntimeError("No active worker")
        
        if hasattr(self._current_worker, 'send_next_task'):
            await self._current_worker.send_next_task(task, context)
        else:
            # Для обычных workers - перезапускаем
            await self.stop()
            await self.start_task(task, context=context)
    
    async def stop(self) -> None:
        """Остановить текущую задачу"""
        if self._watch_task:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
            self._watch_task = None
        
        if self._current_worker:
            await self._current_worker.stop()
            self._current_worker = None
    
    async def _watch_loop(self) -> None:
        """Цикл мониторинга worker'а"""
        if not self._current_worker:
            return
        
        interval = self._current_worker.effective_interval
        logger.info(f"Starting watch loop with {interval}s interval")
        
        while True:
            try:
                await asyncio.sleep(interval)
                
                # Проверить, жив ли worker
                if not await self._current_worker.is_session_alive():
                    logger.warning("Worker session died")
                    self._current_worker.status = WorkerStatus.ERROR
                    if self.on_status_change:
                        await self.on_status_change(WorkerStatus.ERROR)
                    break
                
                # Захватить вывод
                output = await self._current_worker.capture_output()
                output = clean_surrogates(output)
                
                # Получить только новый вывод
                new_output = self._get_new_output(output)
                if new_output and self.on_output:
                    await self.on_output(new_output)
                
                self._last_output = output
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in watch loop: {e}")
    
    def _get_new_output(self, full_output: str) -> str:
        """Получить только новые строки вывода"""
        if not self._last_output:
            return full_output
        
        # Найти новые строки
        if full_output.startswith(self._last_output):
            return full_output[len(self._last_output):]
        
        # Если вывод изменился полностью, вернуть последние строки
        lines = full_output.split('\n')
        last_lines = self._last_output.split('\n')
        
        # Найти первую новую строку
        for i, line in enumerate(lines):
            if line not in last_lines[-50:]:  # Смотрим последние 50 строк
                return '\n'.join(lines[i:])
        
        return ""
    
    async def send_message(self, message: str) -> None:
        """Отправить сообщение в worker (например, ответ на вопрос CLI)"""
        if self._current_worker:
            await self._current_worker.send_input(message)
    
    async def get_output(self) -> str:
        """Получить текущий вывод worker'а
        
        ВАЖНО: Этот метод используется в review_loop.py для получения вывода codex.
        Без него получим ошибку: 'WorkerManager' object has no attribute 'get_output'
        
        Архитектура:
        - Для copilot/droid: используется wait_for_completion() - они сами знают когда готовы
        - Для codex: используется get_output() + LLM проверка завершения
        
        Returns:
            Текущий вывод worker'а или пустая строка если worker не запущен
        """
        if self._current_worker:
            output = await self._current_worker.capture_output()
            return clean_surrogates(output)
        return ""
    
    async def get_status(self) -> Dict:
        """Получить текущий статус"""
        if not self._current_worker:
            return {
                "status": "idle",
                "worker": None,
                "task": None,
                "elapsed": 0,
            }
        
        return {
            "status": self._current_worker.status.value,
            "worker": self._current_worker.WORKER_NAME,
            "task": self._current_worker.current_task,
            "elapsed": self._current_worker.get_elapsed_time(),
            "session_id": self._current_worker.session_id,
        }
    
    async def attach_terminal(self) -> None:
        """Присоединиться к терминалу worker'а"""
        if self._current_worker:
            await self._current_worker.attach()
