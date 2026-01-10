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


# Configurable timeouts
GIT_COMMAND_TIMEOUT = 120  # 2 minutes for most commands
GIT_PUSH_TIMEOUT = 300     # 5 minutes for push (large repos, slow networks)


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
        auto_push: bool = True,
        command_timeout: int = GIT_COMMAND_TIMEOUT,
        push_timeout: int = GIT_PUSH_TIMEOUT,
        dry_run: bool = False
    ):
        self.project_path = Path(project_path)
        self.auto_push = auto_push
        self.command_timeout = command_timeout
        self.push_timeout = push_timeout
        self.dry_run = dry_run
        self._commit_count = 0
    
    def _run_git(self, *args, timeout: Optional[int] = None) -> Tuple[bool, str, str]:
        """Выполнить git команду"""
        if timeout is None:
            timeout = self.command_timeout
        try:
            result = subprocess.run(
                ['git', *args],
                cwd=self.project_path,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            return result.returncode == 0, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return False, "", f"Git command timed out after {timeout}s"
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
        
        # Формируем commit message (sanitize summary)
        safe_summary = summary.replace('"', "'").replace('\n', ' ')[:100] if summary else ""
        commit_msg = f"Step {step_number}, iteration {iteration}"
        if safe_summary:
            commit_msg += f": {safe_summary}"
        
        # Dry run mode - just log what would happen
        if self.dry_run:
            logger.info(f"[DRY RUN] Would commit: {commit_msg}")
            if self.auto_push:
                logger.info("[DRY RUN] Would push to remote")
            return GitResult(
                success=True,
                message=f"[DRY RUN] Would commit: {commit_msg}"
            )
        
        # git add .
        success, _, stderr = self._run_git('add', '.')
        if not success:
            return GitResult(
                success=False,
                message="Failed to stage changes",
                error=stderr
            )
        
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
        success, stdout, stderr = self._run_git('push', timeout=self.push_timeout)
        
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
        """Получить текущую ветку
        
        Returns:
            Branch name, or 'detached:SHORT_SHA' if in detached HEAD state
        """
        success, stdout, _ = self._run_git('rev-parse', '--abbrev-ref', 'HEAD')
        if not success:
            return "unknown"
        
        branch = stdout.strip()
        if branch == "HEAD":
            # Detached HEAD state - get short SHA instead
            success, sha, _ = self._run_git('rev-parse', '--short', 'HEAD')
            if success:
                return f"detached:{sha.strip()}"
            return "detached:unknown"
        
        return branch
    
    def get_last_commit(self) -> str:
        """Получить последний commit"""
        success, stdout, _ = self._run_git('log', '-1', '--oneline')
        return stdout.strip() if success else ""
    
    @property
    def commit_count(self) -> int:
        """Количество коммитов в этой сессии"""
        return self._commit_count
