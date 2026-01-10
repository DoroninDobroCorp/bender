"""
State Persistence - сохранение состояния pipeline

Сохраняет после каждой итерации:
- Текущий шаг и итерация
- Confirmations
- Git commits
- История действий
"""

import json
import shutil
import os
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field, asdict
from contextlib import contextmanager

# Cross-platform file locking
if sys.platform == 'win32':
    import msvcrt
    
    @contextmanager
    def _file_lock_impl(lock_path: Path):
        """Windows file locking using msvcrt"""
        lock_fd = None
        try:
            lock_fd = open(lock_path, 'w')
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
            yield
        finally:
            if lock_fd:
                try:
                    msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
                lock_fd.close()
else:
    import fcntl
    
    @contextmanager
    def _file_lock_impl(lock_path: Path):
        """Unix file locking using fcntl"""
        lock_fd = None
        try:
            lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield
        finally:
            if lock_fd is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)


@dataclass
class IterationLog:
    """Лог одной итерации"""
    step_id: int
    iteration: int
    timestamp: str
    action: str
    has_changes: bool
    confirmations: int
    commit_hash: Optional[str] = None
    notes: str = ""


@dataclass
class PipelineStateData:
    """Данные состояния pipeline"""
    # Идентификация
    run_id: str
    project_path: str
    started_at: str
    
    # Текущее состояние
    current_step: int = 1
    current_iteration: int = 0
    confirmations: int = 0
    status: str = "RUNNING"
    
    # Конфигурация
    target_url: str = ""
    parse_target: str = ""
    
    # История
    iterations: List[Dict] = field(default_factory=list)
    commits: List[str] = field(default_factory=list)
    
    # Recovery
    has_uncommitted_changes: bool = False
    recovery_stash: Optional[str] = None
    
    # Метаданные
    updated_at: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PipelineStateData":
        return cls(**data)


class StatePersistence:
    """Менеджер сохранения состояния"""
    
    STATE_FILE = "pipeline_state.json"
    BACKUP_DIR = "state_backups"
    LOCK_FILE = ".state.lock"
    
    def __init__(self, state_dir: str):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir = self.state_dir / self.BACKUP_DIR
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        
        self._state: Optional[PipelineStateData] = None
        self._lock_file_path = self.state_dir / self.LOCK_FILE
    
    @contextmanager
    def _file_lock(self):
        """Context manager for file locking (cross-platform)"""
        with _file_lock_impl(self._lock_file_path):
            yield
    
    @property
    def state_file(self) -> Path:
        return self.state_dir / self.STATE_FILE
    
    def create_new_run(
        self,
        project_path: str,
        target_url: str = "",
        parse_target: str = ""
    ) -> PipelineStateData:
        """Создать новый run"""
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        self._state = PipelineStateData(
            run_id=run_id,
            project_path=project_path,
            started_at=datetime.now().isoformat(),
            target_url=target_url,
            parse_target=parse_target,
            updated_at=datetime.now().isoformat()
        )
        
        self.save()
        return self._state
    
    def load(self) -> Optional[PipelineStateData]:
        """Загрузить состояние"""
        if not self.state_file.exists():
            return None
        
        try:
            with open(self.state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self._state = PipelineStateData.from_dict(data)
            return self._state
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Failed to load state file, trying backup: {e}")
            return self._load_from_backup()
    
    def save(self):
        """Сохранить состояние (atomic write with file locking)"""
        if self._state is None:
            return
        
        self._state.updated_at = datetime.now().isoformat()
        
        with self._file_lock():
            temp_file = self.state_file.with_suffix('.tmp')
            backup_file = None
            
            try:
                # Write to temp file first
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump(self._state.to_dict(), f, indent=2, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                
                # Create backup of existing state BEFORE replacing
                if self.state_file.exists():
                    backup_file = self._create_backup()
                
                # Atomic rename
                temp_file.rename(self.state_file)
                
            except Exception:
                # Cleanup temp file on failure
                if temp_file.exists():
                    temp_file.unlink()
                raise
    
    def update(
        self,
        current_step: Optional[int] = None,
        current_iteration: Optional[int] = None,
        confirmations: Optional[int] = None,
        status: Optional[str] = None,
        has_uncommitted_changes: Optional[bool] = None,
        recovery_stash: str = ...
    ):
        """Обновить состояние"""
        if self._state is None:
            raise RuntimeError("No state loaded")
        
        if current_step is not None:
            self._state.current_step = current_step
        if current_iteration is not None:
            self._state.current_iteration = current_iteration
        if confirmations is not None:
            self._state.confirmations = confirmations
        if status is not None:
            self._state.status = status
        if has_uncommitted_changes is not None:
            self._state.has_uncommitted_changes = has_uncommitted_changes
        if recovery_stash is not ...:
            self._state.recovery_stash = recovery_stash
        
        self.save()
    
    def log_iteration(
        self,
        step_id: int,
        iteration: int,
        action: str,
        has_changes: bool,
        confirmations: int,
        commit_hash: str = None,
        notes: str = ""
    ):
        """Записать итерацию в лог"""
        if self._state is None:
            raise RuntimeError("No state loaded")
        
        log_entry = IterationLog(
            step_id=step_id,
            iteration=iteration,
            timestamp=datetime.now().isoformat(),
            action=action,
            has_changes=has_changes,
            confirmations=confirmations,
            commit_hash=commit_hash,
            notes=notes
        )
        
        self._state.iterations.append(asdict(log_entry))
        
        if commit_hash:
            self._state.commits.append(commit_hash)
        
        self.save()
    
    def _create_backup(self) -> Optional[Path]:
        """Создать backup текущего состояния
        
        Returns:
            Path to backup file, or None if no backup was created
        """
        if not self.state_file.exists():
            return None
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = self.backup_dir / f"state_{timestamp}.json"
        try:
            shutil.copy2(self.state_file, backup_file)
        except OSError as e:
            import logging
            logging.getLogger(__name__).warning(f"Failed to create backup: {e}")
            return None
        
        # Оставить только последние 10 backups
        backups = sorted(self.backup_dir.glob("state_*.json"))
        for old_backup in backups[:-10]:
            old_backup.unlink(missing_ok=True)
        
        return backup_file
    
    def _load_from_backup(self) -> Optional[PipelineStateData]:
        """Загрузить из последнего backup"""
        backups = sorted(self.backup_dir.glob("state_*.json"))
        if not backups:
            return None
        
        try:
            with open(backups[-1], 'r', encoding='utf-8') as f:
                data = json.load(f)
            self._state = PipelineStateData.from_dict(data)
            import logging
            logging.getLogger(__name__).info(f"Loaded state from backup: {backups[-1].name}")
            return self._state
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Failed to load from backup: {e}")
            return None
    
    def get_state(self) -> Optional[PipelineStateData]:
        """Получить текущее состояние"""
        return self._state
    
    def has_active_run(self) -> bool:
        """Есть ли активный run"""
        state = self.load()
        return state is not None and state.status == "RUNNING"
    
    def list_runs(self) -> List[Dict[str, Any]]:
        """Список всех runs (из backups)"""
        runs = []
        
        # Текущий run
        if self.state_file.exists():
            state = self.load()
            if state:
                runs.append({
                    "run_id": state.run_id,
                    "status": state.status,
                    "current_step": state.current_step,
                    "started_at": state.started_at,
                    "is_current": True
                })
        
        return runs
    
    def clear(self):
        """Очистить состояние"""
        if self.state_file.exists():
            self._create_backup()
            self.state_file.unlink()
        self._state = None
