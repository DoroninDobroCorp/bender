"""
Worker Manager - управление CLI workers
"""

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Type, Callable, Awaitable

from .workers.base import BaseWorker, WorkerConfig, WorkerStatus, WorkerResult
from .workers.copilot import CopilotWorker
from .workers.droid import DroidWorker
from .workers.codex import CodexWorker

logger = logging.getLogger(__name__)


class WorkerType(str, Enum):
    """Типы workers"""
    OPUS = "opus"      # copilot с opus (default)
    DROID = "droid"    # droid для простых задач
    CODEX = "codex"    # codex для сложных задач


WORKER_CLASSES: Dict[WorkerType, Type[BaseWorker]] = {
    WorkerType.OPUS: CopilotWorker,
    WorkerType.DROID: DroidWorker,
    WorkerType.CODEX: CodexWorker,
}


@dataclass
class ManagerConfig:
    """Конфигурация WorkerManager"""
    project_path: Path
    check_interval: float = 30.0
    visible: bool = False
    simple_mode: bool = False
    max_retries: int = 3
    stuck_timeout: float = 300.0


class WorkerManager:
    """Менеджер CLI workers
    
    Управляет жизненным циклом workers, следит за их состоянием,
    перезапускает при необходимости.
    """
    
    def __init__(
        self,
        config: ManagerConfig,
        on_output: Optional[Callable[[str], Awaitable[None]]] = None,
        on_status_change: Optional[Callable[[WorkerStatus], Awaitable[None]]] = None,
    ):
        self.config = config
        self.on_output = on_output
        self.on_status_change = on_status_change
        
        self._current_worker: Optional[BaseWorker] = None
        self._watch_task: Optional[asyncio.Task] = None
        self._last_output: str = ""
    
    @property
    def current_worker(self) -> Optional[BaseWorker]:
        return self._current_worker
    
    @property
    def is_running(self) -> bool:
        return self._current_worker is not None and self._current_worker.status == WorkerStatus.RUNNING
    
    def _create_worker(self, worker_type: WorkerType) -> BaseWorker:
        """Создать worker нужного типа"""
        worker_config = WorkerConfig(
            project_path=self.config.project_path,
            check_interval=self.config.check_interval,
            visible=self.config.visible,
            simple_mode=self.config.simple_mode,
            max_retries=self.config.max_retries,
            stuck_timeout=self.config.stuck_timeout,
        )
        
        worker_class = WORKER_CLASSES[worker_type]
        
        # Для CopilotWorker передаём visible отдельно
        if worker_type == WorkerType.OPUS:
            return worker_class(worker_config, visible=self.config.visible)
        
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
        
        # Для Copilot - не нужен watch loop (он работает синхронно)
        # Для других workers - запустить мониторинг
        if worker_type != WorkerType.OPUS or not hasattr(self._current_worker, 'wait_for_completion'):
            self._watch_task = asyncio.create_task(self._watch_loop())
        
        logger.info(f"Task started with {worker_type.value} worker")
    
    async def wait_for_completion(self, timeout: float = 300) -> tuple:
        """Дождаться завершения задачи (для Copilot worker)"""
        if not self._current_worker:
            return False, ""
        
        if hasattr(self._current_worker, 'wait_for_completion'):
            return await self._current_worker.wait_for_completion(timeout)
        
        # Для других workers - просто ждём
        return False, "Worker does not support wait_for_completion"
    
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
        """Отправить сообщение в worker"""
        if self._current_worker:
            await self._current_worker.send_input(message)
    
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
