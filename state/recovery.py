"""
Recovery - восстановление после сбоев

Поддерживает:
- Resume с места остановки
- Mid-iteration recovery через git stash
- Проверка uncommitted changes
"""

import subprocess
from pathlib import Path
from typing import Optional, Tuple
from dataclasses import dataclass

from .persistence import StatePersistence, PipelineStateData


@dataclass
class RecoveryInfo:
    """Информация для recovery"""
    can_resume: bool
    state: Optional[PipelineStateData]
    has_stash: bool
    stash_name: Optional[str]
    has_uncommitted: bool
    message: str


class RecoveryManager:
    """Менеджер восстановления"""
    
    STASH_PREFIX = "parser_maker_recovery"
    
    def __init__(self, project_path: str, state_dir: str):
        self.project_path = Path(project_path)
        self.persistence = StatePersistence(state_dir)
    
    def check_recovery_needed(self) -> RecoveryInfo:
        """Проверить нужно ли восстановление"""
        # Загрузить состояние
        state = self.persistence.load()
        
        if state is None:
            return RecoveryInfo(
                can_resume=False,
                state=None,
                has_stash=False,
                stash_name=None,
                has_uncommitted=False,
                message="No previous run found"
            )
        
        # Проверить статус
        if state.status == "COMPLETED":
            return RecoveryInfo(
                can_resume=False,
                state=state,
                has_stash=False,
                stash_name=None,
                has_uncommitted=False,
                message="Previous run completed successfully"
            )
        
        # Проверить uncommitted changes
        has_uncommitted = self._has_uncommitted_changes()
        
        # Проверить recovery stash
        has_stash, stash_name = self._check_recovery_stash()
        
        message = f"Can resume from step {state.current_step}, iteration {state.current_iteration}"
        if has_uncommitted:
            message += " (has uncommitted changes)"
        if has_stash:
            message += f" (has stash: {stash_name})"
        
        return RecoveryInfo(
            can_resume=True,
            state=state,
            has_stash=has_stash,
            stash_name=stash_name,
            has_uncommitted=has_uncommitted,
            message=message
        )
    
    def prepare_recovery(self, apply_stash: bool = True) -> Tuple[bool, str]:
        """Подготовить к восстановлению
        
        Args:
            apply_stash: Применить stash если есть
        
        Returns:
            (success, message)
        """
        info = self.check_recovery_needed()
        
        if not info.can_resume:
            return False, info.message
        
        # Если есть uncommitted changes - stash их
        if info.has_uncommitted:
            stash_msg = f"{self.STASH_PREFIX}_step_{info.state.current_step}_iter_{info.state.current_iteration}"
            success = self._stash_changes(stash_msg)
            if success:
                self.persistence.update(
                    has_uncommitted_changes=False,
                    recovery_stash=stash_msg
                )
        
        # Если есть recovery stash и нужно применить
        if info.has_stash and apply_stash:
            success = self._pop_stash()
            if success:
                self.persistence.update(recovery_stash=None)
                return True, f"Applied stash: {info.stash_name}"
        
        return True, "Ready to resume"
    
    def save_for_recovery(self, step_id: int, iteration: int):
        """Сохранить состояние для возможного recovery
        
        Вызывается перед каждой операцией Droid
        """
        # Проверить uncommitted changes
        has_uncommitted = self._has_uncommitted_changes()
        
        self.persistence.update(
            current_step=step_id,
            current_iteration=iteration,
            has_uncommitted_changes=has_uncommitted
        )
    
    def mark_iteration_complete(
        self,
        step_id: int,
        iteration: int,
        action: str,
        has_changes: bool,
        confirmations: int,
        commit_hash: str = None
    ):
        """Отметить итерацию как завершенную"""
        self.persistence.log_iteration(
            step_id=step_id,
            iteration=iteration,
            action=action,
            has_changes=has_changes,
            confirmations=confirmations,
            commit_hash=commit_hash
        )
        
        self.persistence.update(
            current_step=step_id,
            current_iteration=iteration,
            confirmations=confirmations,
            has_uncommitted_changes=False
        )
    
    def mark_step_complete(self, step_id: int):
        """Отметить шаг как завершенный"""
        self.persistence.update(
            current_step=step_id + 1,
            current_iteration=0,
            confirmations=0
        )
    
    def mark_pipeline_complete(self):
        """Отметить pipeline как завершенный"""
        self.persistence.update(status="COMPLETED")
    
    def mark_pipeline_failed(self, reason: str = ""):
        """Отметить pipeline как failed"""
        self.persistence.update(status="FAILED")
        if reason:
            self.persistence.log_iteration(
                step_id=self.persistence.get_state().current_step,
                iteration=self.persistence.get_state().current_iteration,
                action="FAILED",
                has_changes=False,
                confirmations=0,
                notes=reason
            )
    
    def _has_uncommitted_changes(self) -> bool:
        """Проверить есть ли uncommitted changes"""
        try:
            result = subprocess.run(
                ['git', 'status', '--porcelain'],
                cwd=self.project_path,
                capture_output=True,
                text=True,
                timeout=10
            )
            return bool(result.stdout.strip())
        except Exception:
            return False
    
    def _check_recovery_stash(self) -> Tuple[bool, Optional[str]]:
        """Проверить есть ли recovery stash"""
        try:
            result = subprocess.run(
                ['git', 'stash', 'list'],
                cwd=self.project_path,
                capture_output=True,
                text=True,
                timeout=10
            )
            
            for line in result.stdout.split('\n'):
                if self.STASH_PREFIX in line:
                    # Извлечь имя stash
                    parts = line.split(':')
                    if len(parts) >= 2:
                        return True, parts[-1].strip()
            
            return False, None
        except Exception:
            return False, None
    
    def _stash_changes(self, message: str) -> bool:
        """Сохранить изменения в stash"""
        try:
            result = subprocess.run(
                ['git', 'stash', 'push', '-m', message],
                cwd=self.project_path,
                capture_output=True,
                text=True,
                timeout=30
            )
            return result.returncode == 0
        except Exception:
            return False
    
    def _pop_stash(self) -> bool:
        """Применить последний stash"""
        try:
            result = subprocess.run(
                ['git', 'stash', 'pop'],
                cwd=self.project_path,
                capture_output=True,
                text=True,
                timeout=30
            )
            return result.returncode == 0
        except Exception:
            return False
    
    def discard_stash(self) -> bool:
        """Отбросить recovery stash"""
        has_stash, _ = self._check_recovery_stash()
        if not has_stash:
            return True
        
        try:
            result = subprocess.run(
                ['git', 'stash', 'drop'],
                cwd=self.project_path,
                capture_output=True,
                text=True,
                timeout=10
            )
            return result.returncode == 0
        except Exception:
            return False
