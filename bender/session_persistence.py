"""
State Persistence для Bender - збереження активних сесій

Дозволяє відновити моніторинг задач після перезапуску бота.

FIXES:
- Async file I/O (aiofiles) - не блокує event loop
- Atomic writes з fsync - захист від corruption
- Subprocess timeout - не зависає на tmux has-session
- Proper exception logging - не ховає помилки
"""
import json
import logging
import os
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime

import aiofiles

logger = logging.getLogger(__name__)


class SessionPersistence:
    """Менеджер збереження стану активних сесій"""
    
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.sessions_file = self.state_dir / "sessions.json"
        
        # Створюємо директорію якщо не існує
        self.state_dir.mkdir(parents=True, exist_ok=True)
    
    async def save_session(
        self,
        session_id: str,
        user_id: int,
        task: str,
        worker_type: str,
        started_at: str,
        tmux_session: Optional[str] = None,
    ) -> None:
        """Зберегти активну сесію (async для aiofiles)"""
        try:
            sessions = await self._load_sessions()
            
            sessions[session_id] = {
                "user_id": user_id,
                "task": task,
                "worker_type": worker_type,
                "started_at": started_at,
                "tmux_session": tmux_session,
                "updated_at": datetime.now().isoformat(),
            }
            
            await self._save_sessions(sessions)
            logger.info(f"Session {session_id} saved to persistence")
        except Exception as e:
            # FIX #5: Proper exception logging (не просто warning, а full traceback)
            logger.error(f"Failed to save session {session_id}: {e}", exc_info=True)
    
    async def remove_session(self, session_id: str) -> None:
        """Видалити сесію після завершення (async)"""
        try:
            sessions = await self._load_sessions()
            if session_id in sessions:
                del sessions[session_id]
                await self._save_sessions(sessions)
                logger.info(f"Session {session_id} removed from persistence")
        except Exception as e:
            # FIX #5: Proper exception logging
            logger.error(f"Failed to remove session {session_id}: {e}", exc_info=True)
    
    async def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Отримати інформацію про сесію (async)"""
        try:
            sessions = await self._load_sessions()
            return sessions.get(session_id)
        except Exception as e:
            # FIX #5: Log exception instead of silent return
            logger.warning(f"Failed to get session {session_id}: {e}")
            return None
    
    async def get_all_sessions(self) -> Dict[str, Dict[str, Any]]:
        """Отримати всі активні сесії (async)"""
        try:
            return await self._load_sessions()
        except Exception as e:
            # FIX #5: Log exception
            logger.warning(f"Failed to get all sessions: {e}")
            return {}
    
    async def _load_sessions(self) -> Dict[str, Dict[str, Any]]:
        """Завантажити сесії з файлу (async для aiofiles)
        
        FIX #1: Використовуємо aiofiles щоб не блокувати event loop
        """
        if not self.sessions_file.exists():
            return {}
        
        try:
            # FIX #1: Async file I/O
            async with aiofiles.open(self.sessions_file, 'r') as f:
                content = await f.read()
                return json.loads(content)
        except (json.JSONDecodeError, IOError) as e:
            # FIX #5: Log specific exception
            logger.warning(f"Failed to load sessions file: {e}")
            return {}
    
    async def _save_sessions(self, sessions: Dict[str, Dict[str, Any]]) -> None:
        """Зберегти сесії у файл (async + atomic)
        
        FIX #1: Async file I/O
        FIX #4: Atomic write з fsync для гарантії запису на диск
        """
        try:
            # Atomic write: write to temp file, then rename
            temp_file = self.sessions_file.with_suffix('.tmp')
            
            # FIX #1: Async file I/O
            async with aiofiles.open(temp_file, 'w') as f:
                content = json.dumps(sessions, indent=2, ensure_ascii=False)
                await f.write(content)
                await f.flush()
                
                # FIX #4: Форсуємо запис на диск перед rename
                # Це гарантує що дані реально записані, а не в OS cache
                os.fsync(f.fileno())
            
            # FIX #4: Атомарний rename (POSIX гарантує атомарність)
            temp_file.replace(self.sessions_file)
            
        except IOError as e:
            # FIX #5: Full traceback для debug
            logger.error(f"Failed to save sessions file: {e}", exc_info=True)
    
    async def recover_orphaned_sessions(self) -> list[Dict[str, Any]]:
        """Знайти orphaned сесії (tmux живий, але бот забув)
        
        FIX #3: Використовуємо asyncio subprocess з timeout
        
        Returns:
            Список сесій які можна відновити
        """
        recoverable = []
        sessions = await self.get_all_sessions()
        
        for session_id, info in sessions.items():
            tmux_session = info.get('tmux_session')
            if not tmux_session:
                continue
            
            # Перевіряємо чи жива tmux сесія
            try:
                # FIX #3: Async subprocess з timeout
                proc = await asyncio.create_subprocess_exec(
                    'tmux', 'has-session', '-t', tmux_session,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL
                )
                
                # FIX #3: Обов'язковий timeout (2 секунди)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                    if proc.returncode == 0:
                        # Tmux жива - можна відновити
                        recoverable.append({
                            'session_id': session_id,
                            **info
                        })
                except asyncio.TimeoutError:
                    # FIX #5: Log timeout замість silent pass
                    logger.warning(f"Timeout checking tmux session {tmux_session}")
                    proc.kill()
                    await proc.wait()
                    
            except Exception as e:
                # FIX #5: Log exception замість silent pass
                logger.warning(f"Failed to check tmux session {tmux_session}: {e}")
        
        return recoverable
