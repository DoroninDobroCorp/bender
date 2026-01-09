"""
Git Manager - автоматические git операции

Выполняет commit/push после существенных изменений.
При ошибках (конфликты, нет remote) - эскалация к человеку.
"""

import subprocess
import logging
from typing import Optional, Tuple
from pathlib import Path
from dataclasses import dataclass


logger = logging.getLogger(__name__)


@dataclass
class GitResult:
    """Результат git операции"""
    success: bool
    message: str
    needs_human: bool = False
    error: Optional[str] = None


class GitManager:
    """Менеджер git операций"""
    
    def __init__(
        self,
        project_path: str,
        auto_push: bool = True
    ):
        self.project_path = Path(project_path)
        self.auto_push = auto_push
        self._commit_count = 0
    
    def _run_git(self, *args) -> Tuple[bool, str, str]:
        """Выполнить git команду"""
        try:
            result = subprocess.run(
                ['git', *args],
                cwd=self.project_path,
                capture_output=True,
                text=True,
                timeout=60
            )
            return result.returncode == 0, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return False, "", "Git command timed out"
        except Exception as e:
            return False, "", str(e)
    
    def is_git_repo(self) -> bool:
        """Проверить что директория - git репозиторий"""
        success, _, _ = self._run_git('rev-parse', '--git-dir')
        return success
    
    def has_changes(self) -> bool:
        """Проверить есть ли uncommitted changes"""
        success, stdout, _ = self._run_git('status', '--porcelain')
        return success and bool(stdout.strip())
    
    def get_status(self) -> str:
        """Получить git status"""
        success, stdout, stderr = self._run_git('status', '--short')
        return stdout if success else stderr
    
    def commit_and_push(
        self,
        step_number: int,
        iteration: int,
        summary: str = ""
    ) -> GitResult:
        """Сделать commit и push
        
        Args:
            step_number: Номер шага (1-6)
            iteration: Номер итерации
            summary: Краткое описание изменений
        
        Returns:
            GitResult
        """
        if not self.is_git_repo():
            return GitResult(
                success=False,
                message="Not a git repository",
                needs_human=True,
                error="Directory is not a git repository"
            )
        
        if not self.has_changes():
            return GitResult(
                success=True,
                message="No changes to commit"
            )
        
        # git add .
        success, _, stderr = self._run_git('add', '.')
        if not success:
            return GitResult(
                success=False,
                message="Failed to stage changes",
                error=stderr
            )
        
        # Формируем commit message
        commit_msg = f"Step {step_number}, iteration {iteration}"
        if summary:
            commit_msg += f": {summary[:100]}"
        
        # git commit
        success, stdout, stderr = self._run_git('commit', '-m', commit_msg)
        if not success:
            # Проверяем типичные ошибки
            if "nothing to commit" in stderr or "nothing to commit" in stdout:
                return GitResult(
                    success=True,
                    message="Nothing to commit"
                )
            return GitResult(
                success=False,
                message="Failed to commit",
                error=stderr
            )
        
        self._commit_count += 1
        logger.info(f"Committed: {commit_msg}")
        
        # git push (если включено)
        if self.auto_push:
            push_result = self._push()
            if not push_result.success:
                return push_result
        
        return GitResult(
            success=True,
            message=f"Committed and {'pushed' if self.auto_push else 'saved'}: {commit_msg}"
        )
    
    def _push(self) -> GitResult:
        """Выполнить git push"""
        success, stdout, stderr = self._run_git('push')
        
        if success:
            logger.info("Pushed to remote")
            return GitResult(success=True, message="Pushed successfully")
        
        # Анализируем ошибку
        error_text = stderr.lower()
        
        if "no upstream branch" in error_text or "no configured push destination" in error_text:
            return GitResult(
                success=False,
                message="No remote configured",
                needs_human=True,
                error="Please configure git remote: git remote add origin <url>"
            )
        
        if "conflict" in error_text or "rejected" in error_text:
            return GitResult(
                success=False,
                message="Push rejected - conflicts",
                needs_human=True,
                error="Please resolve conflicts manually: git pull --rebase && git push"
            )
        
        if "authentication" in error_text or "permission" in error_text:
            return GitResult(
                success=False,
                message="Authentication failed",
                needs_human=True,
                error="Please check git credentials"
            )
        
        # Неизвестная ошибка
        return GitResult(
            success=False,
            message="Push failed",
            needs_human=True,
            error=stderr
        )
    
    def stash_changes(self, message: str = "") -> GitResult:
        """Сохранить изменения в stash"""
        if not self.has_changes():
            return GitResult(success=True, message="No changes to stash")
        
        stash_msg = message or "auto-stash"
        success, _, stderr = self._run_git('stash', 'push', '-m', stash_msg)
        
        if success:
            return GitResult(success=True, message=f"Stashed: {stash_msg}")
        return GitResult(success=False, message="Failed to stash", error=stderr)
    
    def pop_stash(self) -> GitResult:
        """Восстановить изменения из stash"""
        success, _, stderr = self._run_git('stash', 'pop')
        
        if success:
            return GitResult(success=True, message="Stash applied")
        
        if "No stash entries" in stderr:
            return GitResult(success=True, message="No stash to pop")
        
        return GitResult(
            success=False,
            message="Failed to pop stash",
            needs_human=True,
            error=stderr
        )
    
    def get_current_branch(self) -> str:
        """Получить текущую ветку"""
        success, stdout, _ = self._run_git('rev-parse', '--abbrev-ref', 'HEAD')
        return stdout.strip() if success else "unknown"
    
    def get_last_commit(self) -> str:
        """Получить последний commit"""
        success, stdout, _ = self._run_git('log', '-1', '--oneline')
        return stdout.strip() if success else ""
    
    @property
    def commit_count(self) -> int:
        """Количество коммитов в этой сессии"""
        return self._commit_count
